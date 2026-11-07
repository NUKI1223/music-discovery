import os

from discovery.config import KEYS, load_project_env


def test_dotenv_preserves_process_values_and_never_executes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DISCOVERY_DOTENV", "1")
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENROUTER_MODEL", "explicit-process-model")
    marker = tmp_path / "executed"
    path = tmp_path / ".env"
    path.write_text('OPENROUTER_API_KEY="test-only-placeholder"\n'
                    'OPENROUTER_MODEL=file-model\n'
                    f'OPENROUTER_INPUT_USD_PER_MILLION=$(touch {marker})\n'
                    'DISCOVERY_MODEL_PATH=${OPENROUTER_API_KEY}\n'
                    'UNRELATED_TEST_VARIABLE=should-not-load\n')
    load_project_env(path)
    assert os.environ["OPENROUTER_API_KEY"] == "test-only-placeholder"
    assert os.environ["OPENROUTER_MODEL"] == "explicit-process-model"
    assert os.environ["DISCOVERY_MODEL_PATH"] == "${OPENROUTER_API_KEY}"
    assert not marker.exists()
    assert "UNRELATED_TEST_VARIABLE" not in os.environ
    assert capsys.readouterr() == ("", "")


def test_dotenv_can_be_disabled_and_missing_file_is_optional(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("DISCOVERY_DOTENV", "0")
    path = tmp_path / ".env"
    path.write_text("OPENROUTER_API_KEY=test-only-placeholder\n")
    load_project_env(path)
    assert "OPENROUTER_API_KEY" not in os.environ
    monkeypatch.setenv("DISCOVERY_DOTENV", "1")
    load_project_env(tmp_path / "missing")
    assert "OPENROUTER_API_KEY" not in os.environ
