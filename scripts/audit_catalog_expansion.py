"""Offline coverage/integrity audit; replay only in a disposable account DB copy."""

import argparse
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from discovery.catalog import load_catalog
from discovery.contracts import Decision
from discovery.evals.gold import build_gold_manifest
from discovery.storage import Store, catalog_digest
from discovery.web.chat import get_chat, handle_message
from discovery.web.schemas import MessageRequest


def coverage(catalog, scope):
    ids = catalog.descendant_ids(scope)
    return sum(bool(ids.intersection(release.genre_ids)) for release in catalog.releases)


def journal_fingerprint(connection):
    result = {}
    for table in ("web_chats", "web_messages", "decisions", "feedback", "settings"):
        rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        result[table] = {"count": len(rows), "sha256": hashlib.sha256(
            json.dumps(rows, sort_keys=True).encode()).hexdigest()}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--account-db", type=Path, required=True)
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists")
    started = perf_counter()
    old = load_catalog(args.base)
    new = load_catalog(args.catalog)
    loaded_seconds = perf_counter() - started
    new_releases = {r.id: r for r in new.releases}
    new_artists = {a.id: a for a in new.artists}
    new_genres = {g.id: g for g in new.genres}
    assert {r.id for r in old.releases} <= set(new_releases)
    assert {a.id for a in old.artists} <= set(new_artists)
    for genre in old.genres:
        assert (genre.name, genre.parent_ids) == (new_genres[genre.id].name,
                                                 new_genres[genre.id].parent_ids)
    for release in old.releases:
        current = new_releases[release.id]
        assert (current.title, current.artist_ids, current.year) == (
            release.title, release.artist_ids, release.year)
        assert all(current.links.get(key) == value for key, value in release.links.items())
        assert all(value in current.source_metadata for value in release.source_metadata)
        assert all(value in current.historical_observations for value in release.historical_observations)
        assert all(value in current.observations for value in release.observations)
    by_genre = [{"id": g.id, "name": g.name, "before": coverage(old, g.id),
                 "after": coverage(new, g.id)} for g in old.genres]
    roots = {g.id for g in old.genres if not g.parent_ids}
    vibes = [r for r in new.releases if any(m.vibes for m in r.source_metadata)]
    gold = build_gold_manifest(new)
    with sqlite3.connect(args.account_db.resolve().as_uri() + "?mode=ro", uri=True) as source:
        before = journal_fingerprint(source)
        latest_payload = source.execute("SELECT payload FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        previous = Decision.model_validate_json(latest_payload)
        with TemporaryDirectory(prefix="catalog-expansion-audit-") as directory:
            copied = Path(directory) / "account.sqlite3"
            with sqlite3.connect(copied) as target:
                source.backup(target)
            store = Store(copied)
            original = get_chat(store, args.chat_id)
            previously_shown = {card.release_id for m in original.messages for card in m.cards}
            started = perf_counter()
            response = handle_message(store, args.chat_id,
                MessageRequest(message_id="offline-expansion-audit", apply_filters=True), new)
            first_seconds = perf_counter() - started
            assert response.status == "completed", response.error_code
            assert len(response.cards) == 5
            assert not previously_shown.intersection(card.release_id for card in response.cards)
            decision = store.get_decision("web-" + response.id)
            assert decision is not None
            request_only = set(decision.profile.request_only_artist_ids)
            assert all(not request_only.intersection(new_releases[c.release_id].artist_ids)
                       for c in response.cards)
            assert get_chat(store, args.chat_id).filters == original.filters
            started = perf_counter()
            repeated = handle_message(store, args.chat_id,
                MessageRequest(message_id="offline-expansion-audit", apply_filters=True), new)
            retry_seconds = perf_counter() - started
            assert repeated == response
            assert len(store.decisions()) == before["decisions"]["count"] + 1
            assert len(store.feedback()) == before["feedback"]["count"]
            replay = {"boundary": "disposable_sqlite_backup_no_provider", "filters": original.filters.model_dump(),
                      "previous_candidate_count": len(previous.result.candidate_ids),
                      "candidate_count": len(decision.result.candidate_ids),
                      "response": response.model_dump(mode="json"),
                      "profile_cap": decision.profile.max_widely_known_artists,
                      "new_request_seconds": first_seconds, "idempotent_retry_seconds": retry_seconds,
                      "no_repeat_cards": True, "filters_unchanged": True,
                      "request_only_artists_absent": True, "retry_no_duplicate_decision": True}
        after = journal_fingerprint(source)
        assert before == after, "User may have edited the journal during the audit; recheck separately"
    report = {"timestamp": datetime.now(UTC).isoformat(), "offline": True, "cost_usd": 0,
              "base_catalog_hash": catalog_digest(old), "catalog_hash": catalog_digest(new),
              "catalog_snapshot": new.snapshot_id, "before_releases": len(old.releases),
              "after_releases": len(new.releases), "load_both_catalogs_seconds": loaded_seconds,
              "old_ids_metadata_and_observations_preserved": True, "user_journal_unchanged": before == after,
              "user_journal": after, "root_coverage": [r for r in by_genre if r["id"] in roots],
              "all_genre_coverage": by_genre, "releases_with_vibes": len(vibes),
              "distinct_vibes": sorted({v for r in vibes for m in r.source_metadata for v in m.vibes}),
              "rage_with_vibes": sum("rage" in r.genre_ids for r in vibes),
              "without_primary_genre": sum(not r.genre_ids for r in new.releases),
              "gold_protected_releases": len(gold["protected_release_ids"]),
              "gold_labels_used_for_training": 0, "replay": replay,
              "musical_quality_verified": False, "full_popularity_coverage_verified": False,
              "full_vibe_coverage": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"releases": len(new.releases), "vibes": len(vibes), "rage": coverage(new, "rage"),
                      "root_coverage": report["root_coverage"], "replay_seconds": first_seconds,
                      "cards": [(c.artists, c.title) for c in response.cards]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
