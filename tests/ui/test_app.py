from pathlib import Path

from streamlit.testing.v1 import AppTest

from discovery.catalog import load_catalog
from discovery.config import DEFAULT_CATALOG
from discovery.evals.scenarios import fixture_catalog
from discovery.storage import Store


def button(app, label):
    return next(b for b in app.button if b.label == label)


def test_app_recommend_feedback_restart_and_second_genre(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCOVERY_DOTENV", "0")
    path = tmp_path / "catalog.json"
    path.write_text(fixture_catalog().model_dump_json())
    monkeypatch.setenv("DISCOVERY_CATALOG", str(path))
    monkeypatch.setenv("DISCOVERY_DB", str(tmp_path / "history.db"))
    app_path = str(Path("src/discovery/ui/app.py").resolve())
    app = AppTest.from_file(app_path, default_timeout=20).run()
    assert not app.exception
    button(app, "Найти музыку").click().run()
    assert not app.exception
    first_id = app.session_state["decision_id"]
    button(app, "Прослушал: нравится").click().run()
    assert not app.exception
    button(app, "Найти музыку").click().run()
    assert not app.exception
    assert app.session_state["decision_id"] != first_id
    genre = next(s for s in app.selectbox if s.label == "Жанр")
    genre.set_value("scope-b").run()
    button(app, "Найти музыку").click().run()
    assert not app.exception
    assert any("Second genre" in c.value for c in app.caption)
    restarted = AppTest.from_file(app_path, default_timeout=20).run()
    assert not restarted.exception
    assert (tmp_path / "history-demo.db").exists()
    assert not (tmp_path / "history.db").exists()


def test_real_sample_shows_partial_results_and_rejects_unproven_rarity(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCOVERY_DOTENV", "0")
    monkeypatch.setenv("DISCOVERY_CATALOG", "data/samples/musicbrainz.json")
    monkeypatch.setenv("DISCOVERY_DB", str(tmp_path / "real.db"))
    app = AppTest.from_file(str(Path("src/discovery/ui/app.py").resolve()),
                            default_timeout=20).run()
    button(app, "Найти музыку").click().run()
    assert not app.exception and not app.error
    assert any(b.label == "Прослушал: нравится" for b in app.button)
    strict = next(c for c in app.checkbox if c.label == "Только подтверждённо малоизвестное")
    strict.check().run()
    button(app, "Найти музыку").click().run()
    assert not app.exception and not app.error
    assert not any(b.label == "Прослушал: нравится" for b in app.button)
    assert any("пока нет релизов" in w.value for w in app.warning)
    assert (tmp_path / "real.db").exists()
    assert not (tmp_path / "real-demo.db").exists()


def test_expanded_default_uses_five_verified_artist_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCOVERY_DOTENV", "0")
    monkeypatch.delenv("DISCOVERY_CATALOG", raising=False)
    monkeypatch.setenv("DISCOVERY_DB", str(tmp_path / "expanded.db"))
    app = AppTest.from_file(str(Path("src/discovery/ui/app.py").resolve()),
                            default_timeout=20).run()
    seeds = next(widget for widget in app.multiselect if widget.label == "Любимые исполнители")
    assert len(seeds.value) == 5
    assert "164f0d73-1234-4e2c-8743-d77bf2191051" in seeds.value  # Ye / Kanye West
    assert any("Исторические данные RYM/AOTY" in c.value for c in app.caption)
    # This integration test serializes the full 45k-release/160 MB catalogue.
    # Keep a bounded functional check; 20 seconds is not a latency contract.
    button(app, "Найти музыку").click().run(timeout=60)
    assert not app.exception and not app.error
    decision = Store(tmp_path / "expanded.db").get_decision(app.session_state["decision_id"])
    catalog = load_catalog(DEFAULT_CATALOG)
    assert len(catalog.releases) > 520
    assert decision.taxonomy_version == catalog.taxonomy_version
    assert decision.taxonomy_version.startswith("community-taxonomy-v2+")
    assert decision.result.profile_basis == "artist_prior"
    assert len(decision.result.items) == 5


def test_large_history_work_is_explicit_and_exports_remain_available(tmp_path, monkeypatch):
    """Normal renders and feedback must not reconstruct/export catalogue snapshots."""
    from discovery import cli

    monkeypatch.setenv("DISCOVERY_DOTENV", "0")
    path = tmp_path / "catalog.json"
    path.write_text(fixture_catalog().model_dump_json())
    monkeypatch.setenv("DISCOVERY_CATALOG", str(path))
    monkeypatch.setenv("DISCOVERY_DB", str(tmp_path / "lazy-history.db"))
    exports, training_exports = [], []
    original_export = Store.export
    original_training = cli.training_records

    def export(store):
        result = original_export(store)
        exports.append(result)
        return result

    def training(store):
        result = original_training(store)
        training_exports.append(result)
        return result

    monkeypatch.setattr(Store, "export", export)
    monkeypatch.setattr(cli, "training_records", training)
    app = AppTest.from_file(str(Path("src/discovery/ui/app.py").resolve()),
                           default_timeout=20).run()
    button(app, "Найти музыку").click().run()
    button(app, "Прослушал: нравится").click().run()
    assert not app.exception
    assert not exports and not training_exports
    assert len(app.get("download_button")) == 0

    button(app, "Подготовить экспорт истории").click().run()
    assert not app.exception
    assert len(exports) == 1 and not training_exports
    assert exports[0]["decisions"] and exports[0]["catalogs"]
    assert len(app.get("download_button")) == 1

    button(app, "Подготовить данные для обучения").click().run()
    assert not app.exception
    assert len(exports) == 1 and len(training_exports) == 1
    assert training_exports[0][0]["label"] == 1
    assert len(app.get("download_button")) == 1

    button(app, "Показать статистику").click().run()
    assert not app.exception
    assert len(exports) == 2 and len(training_exports) == 1
    assert any("observed_outcomes_only" in item.value for item in app.json)
