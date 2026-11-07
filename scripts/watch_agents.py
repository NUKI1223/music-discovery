#!/usr/bin/env python3
"""Read-only terminal view of explicitly allowlisted Codex event metadata."""

import argparse
import json
import os
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ITEMS = {"CommandExecution", "FileChange"}
STATUSES = {"completed", "failed", "in_progress", "declined", "cancelled"}
PHASES = {"sources", "offline-rebuild", "lookups", "interrupted", "complete"}
COUNTERS = (
    "queries_done", "query_count", "raw_candidate_count", "selected_release_count",
    "lookups_done", "successful_lookups", "network_requests_this_run", "release_count",
    "artist_count", "ratings_with_value", "known_vote_counts",
)


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.isoformat(timespec="seconds") if parsed.tzinfo else "unknown-time"
    except (TypeError, ValueError):
        return "unknown-time"


def label(value, limit=120):
    """Registry labels are human-maintained; strip terminal control characters."""
    return "".join(c for c in str(value) if c.isprintable())[:limit]


@dataclass
class AgentLog:
    path: Path
    offset: int = 0
    inode: tuple | None = None
    status: str = "no task events"
    turn_id: str | None = None
    started: str = "unknown-time"
    last_task_event: str = "none"
    counts: Counter = field(default_factory=Counter)
    recent: deque = field(default_factory=lambda: deque(maxlen=4))
    invalid_lines: int = 0
    read_status: str = "not read"

    def consume(self, event):
        if not isinstance(event, dict) or event.get("type") != "event_msg":
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        stamp = timestamp(event.get("timestamp"))
        if isinstance(kind, str) and kind in {"task_started", "task_complete", "turn_aborted"}:
            self.counts[kind] += 1
        if kind == "task_started":
            self.turn_id = payload.get("turn_id")
            self.started = stamp
            self.status = "started (recorded)"
        elif kind in {"task_complete", "turn_aborted"}:
            # Completion of an earlier turn cannot close the latest started turn.
            if (self.turn_id is not None and payload.get("turn_id") != self.turn_id):
                return
            if (self.started != "unknown-time" and stamp != "unknown-time"
                    and datetime.fromisoformat(stamp) < datetime.fromisoformat(self.started)):
                return
            self.status = "complete (recorded)" if kind == "task_complete" else "aborted (recorded)"
        elif kind == "item_completed":
            item = payload.get("item")
            if not isinstance(item, dict) or item.get("type") not in ITEMS:
                return
            item_type = item["type"]
            self.counts[item_type] += 1
            status = item.get("status")
            status = status if status in STATUSES else "unknown"
            exit_code = item.get("exit_code")
            exit_label = str(exit_code) if type(exit_code) is int else "unknown"
            suffix = f" exit={exit_label}" if item_type == "CommandExecution" else ""
            self.recent.append(f"{stamp} item_completed {item_type} {status}{suffix}")
            return
        else:
            return
        self.last_task_event = f"{stamp} {kind}"
        self.recent.append(f"{stamp} {kind}")

    def poll(self):
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if self.inode != identity or stat.st_size < self.offset:
                    self.offset = 0
                    self.counts.clear()
                    self.recent.clear()
                    self.status, self.turn_id = "no task events", None
                    self.started = "unknown-time"
                    self.last_task_event = "none"
                    self.invalid_lines = 0
                self.inode = identity
                stream.seek(self.offset)
                while True:
                    line = stream.readline()
                    if not line.endswith(b"\n"):
                        break  # Incomplete last record is retried on next refresh.
                    self.offset = stream.tell()
                    try:
                        self.consume(json.loads(line))
                    except (ValueError, TypeError, AttributeError):
                        self.invalid_lines += 1
                self.read_status = "readable"
        except OSError:
            self.read_status = "unavailable"


def collector_lines(path):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("invalid state")
    except (OSError, ValueError):
        return ["MusicBrainz: state unavailable (no inferred progress)"]
    phase = state.get("phase")
    phase = phase if isinstance(phase, str) and phase in PHASES else "unknown"
    lines = [f"MusicBrainz: phase={phase} updated={timestamp(state.get('updated_at'))}"]
    counters = [f"{key}={state[key]}" for key in COUNTERS if type(state.get(key)) is int]
    lines.extend("  " + " | ".join(counters[i:i + 4]) for i in range(0, len(counters), 4))
    errors = state.get("errors", [])
    if isinstance(errors, list):
        lookup = sum(isinstance(e, dict) and "release_id" in e for e in errors)
        artist = sum(isinstance(e, dict) and "artist" in e for e in errors)
        lines.append(f"  errors={len(errors)} (lookup={lookup}, artist={artist}; details hidden)")
    else:
        lines.append("  errors=unknown (invalid summary)")
    return lines


def render(registry, logs, state_path):
    lines = ["ECC / Codex agent activity — recorded events, not process liveness",
             "Counts cover each session log; no estimated completion percentage.", ""]
    for agent, log in zip(registry, logs):
        log.poll()
        lines.append(f"{label(agent.get('name', '?'))} [{label(agent.get('role', '?'))}]")
        lines.append(f"  Task (registry): {label(agent.get('task', '?'))}")
        lines.append(f"  {log.status}; log={log.read_status}; malformed records={log.invalid_lines}")
        lines.append(f"  Last task event: {log.last_task_event}")
        lines.append("  " + " | ".join(f"{key}={log.counts[key]}" for key in (
            "task_started", "task_complete", "CommandExecution", "FileChange")))
        lines.extend("  " + row for row in log.recent)
        lines.append("")
    lines.extend(collector_lines(state_path))
    lines.append("\nCtrl+C stops this viewer only. Prompts, reasoning, commands and outputs are hidden.")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--refresh", type=float, default=2)
    parser.add_argument("--registry", type=Path, default=ROOT / "docs/agent-tasks.json")
    parser.add_argument("--collector-state", type=Path,
                        default=ROOT / "data/expanded/musicbrainz/state.json")
    args = parser.parse_args(argv)
    if not 0.1 <= args.refresh <= 60:
        parser.error("--refresh must be between 0.1 and 60 seconds")
    logs_by_path = {}
    try:
        while True:
            try:
                registry = json.loads(args.registry.read_text(encoding="utf-8"))["agents"]
                if not isinstance(registry, list) or not all(
                    isinstance(a, dict) and isinstance(a.get("log"), str) for a in registry
                ):
                    raise ValueError("invalid registry")
                paths = [Path(a["log"]).expanduser() for a in registry]
            except (OSError, ValueError, KeyError, TypeError):
                print("Agent registry unavailable or invalid; no task status inferred.", file=sys.stderr)
                return 2
            logs = [logs_by_path.setdefault(path, AgentLog(path)) for path in paths]
            screen = render(registry, logs, args.collector_state)
            if sys.stdout.isatty() and not args.once:
                print("\033[2J\033[H", end="")
            print(screen, flush=True)
            if args.once:
                return 0
            time.sleep(args.refresh)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
