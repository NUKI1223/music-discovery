"""Explicit, single-page HTTPS fetch with robots and access-block enforcement.

No retries, redirects, browser impersonation or scheduling. Network reads only;
successful captures are persisted locally. This is not a grant of data rights.
"""

import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from .common import has_release_markers, page_is_blocked

USER_AGENT = "MusicDiscovery/0.1 (explicit single-page research fetch; robots compliance)"
ROBOTS_AGENT = "MusicDiscovery"
MAX_HTML_BYTES = 2_000_000
MAX_ROBOTS_BYTES = 262_144
TOTAL_TIMEOUT = 45.0
IO_TIMEOUT = 10.0
MIN_INTERVAL = 3.0
_RATE_PATH = Path.home() / ".cache/music-discovery/web-fetch-rate.json"
_THREAD_LOCK = threading.Lock()
_PATHS = {
    "rateyourmusic.com": re.compile(r"/release/(?:album|ep|mixtape)/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+/?"),
    "www.albumoftheyear.org": re.compile(r"/album/[0-9]+-[a-zA-Z0-9_-]+\.php"),
}


class FetchBlocked(ValueError):
    """Safe machine-readable failure, without response bodies or credentials."""

    def __init__(self, code: str, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class FetchResult:
    html_path: Path
    provenance_path: Path
    url: str
    captured_at: str
    sha256: str
    source: str


def validate_url(url: str) -> str:
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise FetchBlocked("INVALID_URL", "A canonical HTTPS album/release URL is required.")
    try:
        parts = urlsplit(url)
        allowed = (
            parts.scheme == "https" and parts.netloc in _PATHS
            and not parts.username and not parts.password and parts.port is None
            and not parts.query and not parts.fragment
            and _PATHS[parts.netloc].fullmatch(parts.path) is not None
        )
    except ValueError:
        allowed = False
    if not allowed:
        raise FetchBlocked("INVALID_URL", "Only canonical RYM release or AOTY album HTTPS URLs are allowed.")
    return url


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise FetchBlocked("DEADLINE", "Fetch deadline reached; no automatic retry.")
    return seconds


def _sleep(delay, deadline):
    if delay >= _remaining(deadline):
        raise FetchBlocked("DEADLINE", "Required rate-limit delay exceeds the fetch deadline.")
    time.sleep(max(0, delay))


def _check_supported_robots(text: str) -> None:
    """Reject rule forms whose precedence urllib.robotparser does not implement.

    This only recognizes enough group structure to reject ambiguity. It does not
    replace RobotFileParser or implement RFC wildcard/longest-match evaluation.
    """
    groups, agents, rules = [], [], []

    def finish():
        nonlocal agents, rules
        if agents:
            groups.append((agents, rules))
        agents, rules = [], []

    for raw in text.splitlines():
        line = unquote(raw.split("#", 1)[0]).strip()
        if not line:
            finish()
            continue
        if ":" not in line:
            raise FetchBlocked("ROBOTS_INVALID", "Unrecognized robots directive; stopped.")
        name, value = (part.strip() for part in line.split(":", 1))
        name = name.casefold()
        if name == "user-agent":
            if rules:
                finish()
            if not value:
                raise FetchBlocked("ROBOTS_INVALID", "Empty robots user-agent; stopped.")
            agents.append(value.casefold())
        elif name in {"allow", "disallow", "crawl-delay", "request-rate"}:
            if not agents:
                raise FetchBlocked("ROBOTS_INVALID", "Robots rule outside an agent group; stopped.")
            rules.append((name, value))
    finish()
    applicable = [rules for agents, rules in groups
                  if any(agent == "*" or agent in ROBOTS_AGENT.casefold() for agent in agents)]
    if len(applicable) > 1:
        raise FetchBlocked("ROBOTS_UNSUPPORTED", "Multiple applicable robots groups; stopped conservatively.")
    for rules in applicable:
        access = [(name, value) for name, value in rules if name in {"allow", "disallow"}]
        if any("*" in value or "$" in value for _, value in access):
            raise FetchBlocked("ROBOTS_UNSUPPORTED", "Wildcard or anchored robots rule is unsupported; stopped.")
        allows = any(name == "allow" or not value for name, value in access)
        denies = any(name == "disallow" and value for name, value in access)
        if allows and denies:
            raise FetchBlocked("ROBOTS_UNSUPPORTED", "Mixed allow/disallow precedence is unsupported; stopped.")
        for name, value in rules:
            if name == "request-rate" and not re.fullmatch(r"[1-9][0-9]{0,8}/[1-9][0-9]{0,8}", value):
                raise FetchBlocked("ROBOTS_INVALID", "Invalid robots request-rate; stopped.")
            if name == "crawl-delay" and not re.fullmatch(r"[0-9]{1,9}", value):
                raise FetchBlocked("ROBOTS_INVALID", "Invalid robots crawl-delay; stopped.")


def _read(client, url, *, cap, interval, deadline, robots=False):
    """Serialize requests across threads/processes using a local shared rate file."""
    if not _THREAD_LOCK.acquire(timeout=_remaining(deadline)):
        raise FetchBlocked("DEADLINE", "Waiting for another fetch exceeded the deadline.")
    try:
        _RATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _RATE_PATH.open("a+", encoding="ascii") as rate:
            while True:
                try:
                    fcntl.flock(rate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    _sleep(0.05, deadline)
            rate.seek(0)
            previous = rate.read()
            if previous:
                try:
                    saved = json.loads(previous)
                    last = float(saved["timestamp"])
                    previous_interval = float(saved["interval"])
                    if not math.isfinite(last) or not math.isfinite(previous_interval) or previous_interval < 0:
                        raise ValueError
                except (ValueError, TypeError, KeyError):
                    raise FetchBlocked("RATE_STATE_INVALID", "Local fetch rate state is invalid.") from None
                _sleep(max(0, max(interval, previous_interval) - (time.time() - last)), deadline)
            rate.seek(0)
            rate.truncate()
            rate.write(json.dumps({"timestamp": time.time(), "interval": interval}))
            rate.flush()
            timeout = min(IO_TIMEOUT, _remaining(deadline))
            request = httpx.Request("GET", url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/plain" if robots else "text/html,application/xhtml+xml",
                "Accept-Encoding": "identity",
            }, extensions={"timeout": {name: timeout for name in ("connect", "read", "write", "pool")}})
            # Build a fresh Request so an injected client's cookies, default headers,
            # auth and base_url do not change the explicitly authorized destination.
            response = client.send(request, stream=True, follow_redirects=False, auth=None)
            try:
                status = response.status_code
                if status != 200:
                    code = "ROBOTS_UNAVAILABLE" if robots else "HTTP_BLOCKED"
                    if 300 <= status < 400:
                        code = "REDIRECT_BLOCKED"
                    raise FetchBlocked(code, f"{'Robots' if robots else 'Page'} HTTP {status}; stopped.",
                                       status_code=status)
                if response.headers.get("cf-mitigated", "").lower() == "challenge":
                    raise FetchBlocked("CHALLENGE", "Access challenge detected; stopped.", status_code=status)
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                expected = {"text/plain"} if robots else {"text/html", "application/xhtml+xml"}
                if mime not in expected:
                    raise FetchBlocked("CONTENT_TYPE", "Unexpected response content type; stopped.")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise FetchBlocked("CONTENT_ENCODING", "Unexpected compressed response; stopped.")
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if not 0 <= int(content_length) <= cap:
                            raise ValueError
                    except ValueError:
                        raise FetchBlocked("SIZE_LIMIT", "Response length exceeds the permitted bound.") from None
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    _remaining(deadline)
                    size += len(chunk)
                    if size > cap:
                        raise FetchBlocked("SIZE_LIMIT", "Decoded response exceeds the permitted bound.")
                    chunks.append(chunk)
                _remaining(deadline)
                return b"".join(chunks), response.headers.get("content-type", "")
            finally:
                response.close()
    except httpx.HTTPError:
        raise FetchBlocked("NETWORK_ERROR", "Network read failed; no automatic retry.") from None
    except OSError:
        raise FetchBlocked("RATE_STATE_UNAVAILABLE", "Local rate limiter unavailable; no request allowed.") from None
    finally:
        _THREAD_LOCK.release()


def _real_page(body: bytes, host: str):
    soup = BeautifulSoup(body, "html.parser")
    source = "rym" if host == "rateyourmusic.com" else "aoty"
    if page_is_blocked(soup, source):
        raise FetchBlocked("CHALLENGE", "Access challenge detected in HTML; stopped.")
    if soup.html is None or soup.title is None or not has_release_markers(soup, source):
        raise FetchBlocked("NOT_RELEASE_PAGE", "HTML does not contain recognizable album-page structure.")


def fetch_html(url: str, outputdir: str | Path, *, client: httpx.Client | None = None) -> FetchResult:
    """Fetch one explicit page after robots approval; persist only verified HTML.

    45s overall streaming deadline, with each blocking I/O bounded to at most 10s.
    Deadline is rechecked after I/O; wall time can exceed 45s by one I/O timeout.
    Rate file is shared by processes for this OS user. Robots are fetched on every
    invocation; a cached earlier permission is never substituted for current robots.
    """
    url = validate_url(url)
    host = urlsplit(url).netloc
    robots_url = f"https://{host}/robots.txt"
    deadline = time.monotonic() + TOTAL_TIMEOUT
    owned = client is None
    client = client or httpx.Client(follow_redirects=False, trust_env=False)
    try:
        robots, _ = _read(client, robots_url, cap=MAX_ROBOTS_BYTES,
                          interval=MIN_INTERVAL, deadline=deadline, robots=True)
        try:
            robots_text = robots.decode("utf-8-sig", errors="strict")
        except UnicodeError:
            raise FetchBlocked("ROBOTS_INVALID", "Robots is not valid UTF-8 text; stopped.") from None
        if not re.search(r"(?im)^\s*user-agent\s*:", robots_text):
            raise FetchBlocked("ROBOTS_INVALID", "Robots has no recognizable agent directives; stopped.")
        _check_supported_robots(robots_text)
        parser = RobotFileParser(robots_url)
        parser.parse(robots_text.splitlines())
        if not parser.can_fetch(ROBOTS_AGENT, url):
            raise FetchBlocked("ROBOTS_DENIED", "Source robots.txt disallows this client; stopped.")
        checked_at = datetime.now(UTC).isoformat()
        interval = max(MIN_INTERVAL, parser.crawl_delay(ROBOTS_AGENT) or 0)
        rate = parser.request_rate(ROBOTS_AGENT)
        if rate:
            if rate.requests <= 0 or rate.seconds <= 0:
                raise FetchBlocked("ROBOTS_INVALID", "Invalid robots request-rate; stopped.")
            interval = max(interval, rate.seconds / rate.requests)
        body, content_type = _read(client, url, cap=MAX_HTML_BYTES,
                                   interval=interval, deadline=deadline)
        _real_page(body, host)
        captured_at = datetime.now(UTC).isoformat()
    finally:
        if owned:
            client.close()
    digest = hashlib.sha256(body).hexdigest()
    source = "rym" if host == "rateyourmusic.com" else "aoty"
    outputdir = Path(outputdir)
    outputdir.mkdir(parents=True, exist_ok=True)
    capture_dir = Path(tempfile.mkdtemp(prefix=f"{source}-{digest[:12]}-", dir=outputdir))
    html_path, provenance_path = capture_dir / "page.html", capture_dir / "provenance.json"
    provenance = {
        "source": source, "url": url, "captured_at": captured_at, "http_status": 200,
        "sha256": digest, "byte_count": len(body), "content_type": content_type,
        "user_agent": USER_AGENT, "robots_url": robots_url, "robots_checked_at": checked_at,
        "robots_sha256": hashlib.sha256(robots).hexdigest(), "robots_allowed": True,
        "rights_status": "not_established_by_technical_access", "is_fixture": False,
        "capture_method": "explicit_single_page_no_redirects_no_retries",
    }
    try:
        html_path.write_bytes(body)
        provenance_path.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    except OSError:
        html_path.unlink(missing_ok=True)
        provenance_path.unlink(missing_ok=True)
        os.rmdir(capture_dir)
        raise
    return FetchResult(html_path, provenance_path, url, captured_at, digest, source)
