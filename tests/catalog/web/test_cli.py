"""Offline end-to-end export checks; all music values are authored fixtures."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def run_parse(source, html, url, output, observed="2026-10-28T12:00:00+00:00"):
    return subprocess.run(
        [sys.executable, "-m", "discovery.catalog.web", "parse", "--source", source,
         "--html", str(html), "--url", url, "--observed-at", observed,
         "--output", str(output), "--fixture"], capture_output=True, text=True, timeout=10,
    )


@pytest.mark.parametrize("source,fixture,url,rating,scale", [
    ("rym", "rym/release.html", "https://rateyourmusic.com/release/album/demo/test/", 3.75, 5),
    ("aoty", "aoty_synthetic_album.html",
     "https://www.albumoftheyear.org/album/987654-synthetic-artist-test-release.php", 88.45, 100),
])
def test_export_preserves_source_facts_and_fixture_boundary(tmp_path, source, fixture, url,
                                                          rating, scale):
    html = FIXTURES / fixture
    output = tmp_path / "release.json"
    result = run_parse(source, html, url, output)
    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text())
    assert payload["is_fixture"] is True
    assert payload["live_parser_verified"] is False
    assert payload["personal_labels"] == 0
    assert payload["dataset_kind"] == "unmatched_source_release"
    assert payload["input_sha256"] == hashlib.sha256(html.read_bytes()).hexdigest()
    assert payload["record"]["rating"] == rating
    assert payload["record"]["rating_scale"] == scale
    assert payload["record"]["rating_count"] == 1234
    assert payload["record"]["field_evidence"]
    original = output.read_bytes()
    repeated = run_parse(source, html, url, output)
    assert repeated.returncode == 2
    assert json.loads(repeated.stderr)["code"] == "FileExistsError"
    assert output.read_bytes() == original


def test_challenge_is_not_exported(tmp_path):
    html = tmp_path / "challenge.html"
    html.write_text("<html><title>Just a moment...</title></html>")
    output = tmp_path / "output.json"
    result = run_parse("rym", html, "https://rateyourmusic.com/release/album/demo/test/", output)
    assert result.returncode == 2
    assert json.loads(result.stderr)["code"] == "BLOCKED_PAGE"
    assert not output.exists()


def test_capture_time_requires_timezone(tmp_path):
    output = tmp_path / "output.json"
    result = run_parse("rym", FIXTURES / "rym/release.html",
                       "https://rateyourmusic.com/release/album/demo/test/", output,
                       observed="2026-10-28T12:00:00")
    assert result.returncode == 2
    assert not output.exists()


def test_fetch_cli_reports_block_without_export(tmp_path, monkeypatch, capsys):
    from discovery.catalog.web import __main__ as cli
    from discovery.catalog.web import fetch

    def blocked(*args):
        raise fetch.FetchBlocked("ROBOTS_UNAVAILABLE", "Robots HTTP 403; stopped.", status_code=403)

    monkeypatch.setattr(fetch, "fetch_html", blocked)
    monkeypatch.setattr(sys, "argv", ["web", "fetch", "--url",
                        "https://www.albumoftheyear.org/album/123-test.php",
                        "--output-dir", str(tmp_path / "captures")])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    result = json.loads(capsys.readouterr().err)
    assert result["status"] == "blocked"
    assert result["http_status"] == 403
    assert not (tmp_path / "captures").exists()
