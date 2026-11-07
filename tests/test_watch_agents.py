import importlib.util
import json
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "watch_agents", Path(__file__).resolve().parents[1] / "scripts/watch_agents.py",
)
watch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = watch
SPEC.loader.exec_module(watch)


def event(kind, second=1, **payload):
    return {"timestamp": f"2026-10-28T12:00:{second:02d}+00:00", "type": "event_msg",
            "payload": {"type": kind, **payload}}


def test_safe_stdout_includes_metadata_but_no_reasoning_prompts_or_command_secrets(tmp_path, capsys):
    secret = "TOP_SECRET_MUST_NOT_APPEAR"
    log = tmp_path / "agent.jsonl"
    records = [
        event("task_started", turn_id="current", full_prompt=secret),
        event("item_completed", item={"type": "Reasoning", "summary_text": secret,
                                      "raw_content": secret}),
        event("item_completed", item={"type": "CommandExecution", "command": secret,
                                      "stdout": secret, "stderr": secret,
                                      "status": "failed", "exit_code": 7}),
        event("item_completed", item={"type": "FileChange", "changes": {secret: secret},
                                      "status": "completed", "stdout": secret}),
        event("task_complete", turn_id="current", last_agent_message=secret),
    ]
    log.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"agents": [{"name": "test", "role": "review",
                                              "task": "test visibility", "log": str(log)}]}))
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"phase": "lookups", "updated_at": "2026-10-28T12:00:01Z",
                                 "lookups_done": 3, "errors": [{"release_id": secret,
                                                              "error": secret}]}))
    assert watch.main(["--once", "--registry", str(registry),
                       "--collector-state", str(state)]) == 0
    rendered = capsys.readouterr().out
    assert secret not in rendered
    assert "CommandExecution=1" in rendered and "FileChange=1" in rendered
    assert "failed exit=7" in rendered and "errors=1 (lookup=1" in rendered
    assert "lookups_done=3" in rendered and "complete (recorded)" in rendered


def test_new_start_is_not_closed_by_old_turn_completion(tmp_path):
    log = watch.AgentLog(tmp_path / "unused")
    log.consume(event("task_started", 1, turn_id="old"))
    log.consume(event("task_complete", 2, turn_id="old"))
    log.consume(event("task_started", 3, turn_id="new"))
    assert log.status == "started (recorded)"
    log.consume(event("task_complete", 4, turn_id="old"))
    assert log.status == "started (recorded)"
    log.consume(event("task_complete", 2, turn_id="new"))
    assert log.status == "started (recorded)"
    log.consume(event("task_complete", 5, turn_id="new"))
    assert log.status == "complete (recorded)"
    assert log.counts["task_complete"] == 4  # Older events count without closing the new turn.


def test_incremental_partial_lines_and_replaced_logs_do_not_double_count(tmp_path):
    path = tmp_path / "agent.jsonl"
    first = json.dumps(event("task_started", turn_id="one"))
    second = json.dumps(event("item_completed", item={"type": "FileChange", "status": "completed"}))
    path.write_text(first + "\n" + second[:20])
    log = watch.AgentLog(path)
    log.poll()
    log.poll()
    assert log.counts["task_started"] == 1 and log.counts["FileChange"] == 0
    with path.open("a") as stream:
        stream.write(second[20:] + "\n")
    log.poll()
    assert log.counts["FileChange"] == 1
    replacement = tmp_path / "replacement"
    replacement.write_text(json.dumps(event("task_started", turn_id="two")) + "\n")
    replacement.replace(path)
    log.poll()
    assert log.turn_id == "two" and log.counts["FileChange"] == 0


def test_malformed_and_missing_files_do_not_print_raw_input(tmp_path):
    path = tmp_path / "agent.jsonl"
    path.write_text('TOP_SECRET_MUST_NOT_APPEAR\n{"type":"event_msg","payload":null}\n')
    log = watch.AgentLog(path)
    log.poll()
    assert log.invalid_lines == 1 and not log.recent
    assert watch.collector_lines(tmp_path / "missing") == [
        "MusicBrainz: state unavailable (no inferred progress)",
    ]
    path.unlink()
    log.poll()
    assert log.read_status == "unavailable"
