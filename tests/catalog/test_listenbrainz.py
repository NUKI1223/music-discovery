"""Offline tests for ListenBrainz listener counts; every HTTP call goes to a MockTransport."""

import fcntl
import json
import re
import signal
import subprocess
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from discovery.catalog import listenbrainz, musicbrainz, save_catalog
from discovery.catalog.listenbrainz import (
    MATCH_METHOD,
    ListenBrainzClient,
    apply_listeners,
    build_query,
    choose_release_group,
    collect_listeners,
    load_ids,
    load_with_listeners,
    match_catalog,
    normalize,
    release_group_ids,
    status,
)
from discovery.catalog.musicbrainz import MusicBrainzClient
from discovery.contracts import Artist, Catalog, Genre, Observation, RequestIntent, UserProfile
from discovery.ranking import score_candidates


def uid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def make_catalog(releases, artists=None, snapshot_id="snap-1") -> Catalog:
    from discovery.contracts import Release

    artists = artists or [Artist(id="a", name="Artist A"), Artist(id="b", name="Artist B")]
    return Catalog(
        snapshot_id=snapshot_id,
        genres=[Genre(id="hip", name="Hip hop")],
        artists=artists,
        releases=[
            Release(
                id=rid,
                title=title,
                artist_ids=artist_ids,
                genre_ids=["hip"],
                year=year,
                observations=list(observations),
            )
            for rid, title, artist_ids, year, observations in releases
        ],
    )


def rel(rid, title="Alpha", artist_ids=("a",), year=2020, observations=()):
    return (rid, title, list(artist_ids), year, observations)


def rg(n, title="Alpha", artist="Artist A", date="", primary="Album", secondary=None, credit=None):
    row = {
        "id": uid(n),
        "title": title,
        "primary-type": primary,
        "first-release-date": date,
        "artist-credit": credit or [{"name": artist, "artist": {"name": artist}}],
    }
    if secondary is not None:
        row["secondary-types"] = secondary
    return row


def credit(name, join=""):
    return {"name": name, "joinphrase": join, "artist": {"name": name}}


# ---------------------------------------------------------------- normalize


def test_normalize_ignores_case_accents_punctuation_and_spaces():
    assert normalize("Beyoncé") == normalize("beyonce")
    assert normalize("  The  Dark-Side, of the Moon! ") == normalize("the dark side of the moon")


def test_normalize_keeps_japanese_voicing_marks():
    assert normalize("ば") != normalize("は")
    assert normalize("が") != normalize("か")
    assert normalize("Beyoncé") == normalize("beyonce")


def test_normalize_symbol_only_names_stay_distinct_and_nonempty():
    assert normalize("!!!") and normalize("???")
    assert normalize("!!!") != normalize("???")
    assert normalize("!!!") == normalize("  !!!  ")


# ---------------------------------------------------------------- build_query


def test_build_query_format_and_dedup():
    query = build_query("Artist A", ["A", "B", "A"])
    assert query == 'artist:"Artist A" AND (releasegroup:"A" OR releasegroup:"B")'


def test_build_query_escapes_quotes_and_backslashes():
    query = build_query('Say "Hi"\\', ['The "One"', "back\\slash"])
    assert query == (
        'artist:"Say \\"Hi\\"\\\\" AND '
        '(releasegroup:"The \\"One\\"" OR releasegroup:"back\\\\slash")'
    )


# ---------------------------------------------------------------- choose_release_group


def test_choose_exact_match_ignores_case_and_accents():
    rows = [rg(1, title="Café Noir", artist="Beyoncé")]
    assert choose_release_group("cafe noir", "BEYONCE", None, rows) == (uid(1), "matched")


def test_choose_other_title_or_artist_is_absent():
    rows = [rg(1)]
    assert choose_release_group("Other", "Artist A", None, rows) == (None, "absent")
    assert choose_release_group("Alpha", "Artist Z", None, rows) == (None, "absent")
    assert choose_release_group("Alpha", "Artist A", None, []) == (None, "absent")


def test_choose_two_different_ids_with_same_rank_are_ambiguous():
    assert choose_release_group("Alpha", "Artist A", None, [rg(1), rg(2)]) == (None, "ambiguous")


def test_choose_same_id_repeated_is_not_ambiguous():
    assert choose_release_group("Alpha", "Artist A", None, [rg(1), rg(1)]) == (uid(1), "matched")


def test_choose_prefers_same_year():
    rows = [rg(1, date="2019-05-01"), rg(2, date="2020-01-01")]
    assert choose_release_group("Alpha", "Artist A", 2020, rows) == (uid(2), "matched")
    assert choose_release_group("Alpha", "Artist A", None, rows) == (None, "ambiguous")
    # A candidate more than a year away is dropped, so the 2020 row is the only one left.
    assert choose_release_group("Alpha", "Artist A", 2021, rows) == (uid(2), "matched")


def test_choose_rejects_candidates_more_than_a_year_away():
    rows = [rg(1, date="1999-01-01")]
    assert choose_release_group("Alpha", "Artist A", 2020, rows) == (None, "absent")
    assert choose_release_group("Alpha", "Artist A", 2002, [rg(1, date="2000")]) == (None, "absent")
    assert choose_release_group("Alpha", "Artist A", None, rows) == (uid(1), "matched")


def test_choose_accepts_year_off_by_one_and_undated_candidates():
    assert choose_release_group("Alpha", "Artist A", 2020, [rg(1, date="2021-01-01")]) == (
        uid(1),
        "matched",
    )
    assert choose_release_group("Alpha", "Artist A", 2020, [rg(1, date="2019")]) == (
        uid(1),
        "matched",
    )
    assert choose_release_group("Alpha", "Artist A", 2020, [rg(1, date="")]) == (uid(1), "matched")
    assert choose_release_group("Alpha", "Artist A", 2020, [rg(1, date="abcd")]) == (
        uid(1),
        "matched",
    )


def test_choose_year_filter_keeps_credit_match_when_whole_match_has_wrong_year():
    whole = rg(1, date="1990-01-01")
    collab = rg(2, date="2020-01-01", credit=[credit("Artist A", " & "), credit("Artist B")])
    assert choose_release_group("Alpha", "Artist A", 2020, [whole, collab]) == (uid(2), "matched")


def test_choose_empty_title_is_absent():
    rows = [rg(1, title=""), rg(2, title="  ")]
    assert choose_release_group("", "Artist A", None, rows) == (None, "absent")
    assert choose_release_group("   ", "Artist A", None, rows) == (None, "absent")


@pytest.mark.parametrize(
    "bad_credit",
    [
        "Artist A",
        {"name": "Artist A"},
        None,
        ["Artist A", 5, None],
        [{"name": "Artist A", "artist": "Artist A"}],
        [{"name": "Artist A", "artist": ["Artist A"]}],
    ],
)
def test_choose_survives_malformed_artist_credit(bad_credit):
    row = rg(1) | {"artist-credit": bad_credit}
    assert choose_release_group("Alpha", "Artist A", None, [row]) == (None, "absent")


def test_choose_ignores_malformed_credit_elements_but_keeps_valid_ones():
    row = rg(1) | {"artist-credit": ["junk", {"name": "Artist A", "artist": "x"}, credit("Artist A")]}
    assert choose_release_group("Alpha", "Artist A", None, [row]) == (uid(1), "matched")


def test_choose_prefers_album_over_single_and_other():
    rows = [rg(1, primary="Single"), rg(2, primary="Album"), rg(3, primary="Broadcast")]
    assert choose_release_group("Alpha", "Artist A", None, rows) == (uid(2), "matched")
    rows = [rg(1, primary="Single"), rg(2, primary="EP")]
    assert choose_release_group("Alpha", "Artist A", None, rows) == (uid(2), "matched")


def test_choose_prefers_row_without_secondary_types():
    rows = [rg(1, secondary=["Live"]), rg(2, secondary=[]), rg(3)]
    assert choose_release_group("Alpha", "Artist A", None, rows) == (None, "ambiguous")
    rows = [rg(1, secondary=["Live"]), rg(2)]
    assert choose_release_group("Alpha", "Artist A", None, rows) == (uid(2), "matched")


def test_choose_album_with_secondary_beats_plain_single():
    rows = [rg(1, primary="Single"), rg(2, secondary=["Compilation"])]
    assert choose_release_group("Alpha", "Artist A", None, rows) == (uid(2), "matched")


def test_choose_full_credit_beats_one_of_several_credits():
    collab = rg(1, credit=[credit("Artist A", " & "), credit("Artist B")])
    solo = rg(2)
    assert choose_release_group("Alpha", "Artist A", None, [collab, solo]) == (uid(2), "matched")
    assert choose_release_group("Alpha", "Artist A & Artist B", None, [solo, collab]) == (
        uid(1),
        "matched",
    )


def test_choose_comma_joined_credit_matches_whole_credit():
    collab = rg(1, credit=[credit("Artist A", " feat. "), credit("Artist B")])
    solo = rg(2, artist="Artist A")
    assert choose_release_group("Alpha", "Artist A, Artist B", None, [solo, collab]) == (
        uid(1),
        "matched",
    )


def test_choose_single_credit_of_collaboration_matches_when_nothing_better():
    collab = rg(1, credit=[credit("Artist A", " & "), credit("Artist B")])
    assert choose_release_group("Alpha", "Artist B", None, [collab]) == (uid(1), "matched")


def test_choose_ignores_bad_ids_and_non_dict_rows():
    bad = rg(1)
    bad["id"] = "not-a-uuid"
    missing = rg(3)
    del missing["id"]
    rows = [None, "text", 5, bad, missing, rg(2)]
    assert choose_release_group("Alpha", "Artist A", None, rows) == (uid(2), "matched")
    assert choose_release_group("Alpha", "Artist A", None, [bad, None]) == (None, "absent")


# ---------------------------------------------------------------- MusicBrainz matching


@pytest.fixture
def mb(monkeypatch):
    monkeypatch.setattr(musicbrainz.time, "sleep", lambda *_: None)
    clients = []

    def make(handler):
        client = MusicBrainzClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


class FakeMusicBrainz:
    """Answers each query with all stored rows of the artist named in it."""

    def __init__(self, db):
        self.db = db
        self.queries = []

    def __call__(self, request):
        query = request.url.params["query"]
        self.queries.append(query)
        artist = re.match(r'artist:"(.*?)" AND', query).group(1)
        rows = self.db.get(artist, [])
        return httpx.Response(200, json={"count": len(rows), "release-groups": rows})


def three_artist_catalog():
    artists = [Artist(id=x, name=f"Artist {x.upper()}") for x in "abc"]
    return make_catalog(
        [
            rel("a1", "Alpha", ["a"]),
            rel("b1", "Gamma", ["b"]),
            rel("c1", "Delta", ["c"]),
        ],
        artists,
    )


def test_match_catalog_searches_only_unknown_releases_and_batches_by_artist(mb, tmp_path):
    catalog = make_catalog(
        [
            rel("a1", "Alpha", ["a"]),
            rel("a2", "Beta", ["a"]),
            rel("b1", "Gamma", ["b"]),
            rel(uid(9), "Own id", ["b"]),
            rel("pre", "Known", ["b"]),
            rel("old", "Failed before", ["b"]),
        ]
    )
    ids_path = tmp_path / "ids.json"
    ids_path.write_text(
        json.dumps(
            {
                "snapshot_id": "snap-1",
                "method": MATCH_METHOD,
                "matched": {"pre": uid(50)},
                "unmatched": {"old": "absent"},
            }
        )
    )
    fake = FakeMusicBrainz(
        {
            "Artist A": [rg(1, "Alpha", "Artist A"), rg(2, "Beta", "Artist A")],
            "Artist B": [rg(3, "Gamma", "Artist B")],
        }
    )
    client = mb(fake)
    report = match_catalog(catalog, ids_path, client)
    assert fake.queries == [
        'artist:"Artist A" AND (releasegroup:"Alpha" OR releasegroup:"Beta")',
        'artist:"Artist B" AND (releasegroup:"Gamma")',
    ]
    assert client.network_requests == 2
    assert report["phase"] == "complete"
    assert report["requests_this_run"] == 2
    assert report["remaining"] == 0
    state = json.loads(ids_path.read_text())
    assert state["matched"] == {"pre": uid(50), "a1": uid(1), "a2": uid(2), "b1": uid(3)}
    assert state["unmatched"] == {"old": "absent"}
    assert state["snapshot_id"] == "snap-1"
    assert datetime.fromisoformat(state["updated_at"]).tzinfo is not None


def test_match_catalog_records_unmatched_status_and_second_run_is_free(mb, tmp_path):
    catalog = make_catalog(
        [rel("a1", "Alpha", ["a"]), rel("a2", "Beta", ["a"]), rel("a3", "Gamma", ["a"])]
    )
    ids_path = tmp_path / "ids.json"
    fake = FakeMusicBrainz(
        {
            "Artist A": [
                rg(1, "Alpha", "Artist A"),
                rg(2, "Gamma", "Artist A"),
                rg(3, "Gamma", "Artist A"),
            ]
        }
    )
    client = mb(fake)
    report = match_catalog(catalog, ids_path, client)
    assert report["matched"] == 1 and report["absent"] == 1 and report["ambiguous"] == 1
    state = json.loads(ids_path.read_text())
    assert state["matched"] == {"a1": uid(1)}
    assert state["unmatched"] == {"a2": "absent", "a3": "ambiguous"}

    again = match_catalog(catalog, ids_path, client)
    assert again["requests_this_run"] == 0 and again["phase"] == "complete"
    assert len(fake.queries) == 1


def test_match_catalog_max_requests_pauses_and_next_run_resumes(mb, tmp_path):
    catalog = three_artist_catalog()
    ids_path = tmp_path / "ids.json"
    fake = FakeMusicBrainz(
        {
            "Artist A": [rg(1, "Alpha", "Artist A")],
            "Artist B": [rg(2, "Gamma", "Artist B")],
            "Artist C": [rg(3, "Delta", "Artist C")],
        }
    )
    client = mb(fake)
    first = match_catalog(catalog, ids_path, client, max_requests=2)
    assert first["phase"] == "paused"
    assert first["requests_this_run"] == 2 and first["remaining"] == 1
    assert set(json.loads(ids_path.read_text())["matched"]) == {"a1", "b1"}

    second = match_catalog(catalog, ids_path, client, max_requests=2)
    assert second["phase"] == "complete"
    assert second["requests_this_run"] == 1 and second["remaining"] == 0
    assert set(json.loads(ids_path.read_text())["matched"]) == {"a1", "b1", "c1"}
    assert len(fake.queries) == 3


def test_match_catalog_checkpoints_and_reports_progress(mb, tmp_path, monkeypatch):
    monkeypatch.setattr(listenbrainz, "CHECKPOINT_REQUESTS", 1)
    catalog = three_artist_catalog()
    reports = []
    match_catalog(
        catalog, tmp_path / "ids.json", mb(FakeMusicBrainz({})), progress=reports.append
    )
    assert [r["phase"] for r in reports] == ["matching", "matching", "matching", "complete"]
    assert reports[-1]["absent"] == 3


def test_match_catalog_batches_at_most_eight_titles_per_query(mb, tmp_path):
    catalog = make_catalog([rel(f"a{i}", f"Title {i}", ["a"]) for i in range(9)])
    fake = FakeMusicBrainz({})
    match_catalog(catalog, tmp_path / "ids.json", mb(fake))
    assert len(fake.queries) == 2
    assert fake.queries[0].count("releasegroup:") == 8
    assert fake.queries[1].count("releasegroup:") == 1


def test_match_catalog_duplicate_titles_of_one_artist_are_queried_once(mb, tmp_path):
    catalog = make_catalog([rel("a1", "Alpha", ["a"], 2019), rel("a2", "Alpha", ["a"], 2020)])
    fake = FakeMusicBrainz({"Artist A": [rg(1, date="2019-01-01"), rg(2, date="2020-01-01")]})
    match_catalog(catalog, tmp_path / "ids.json", mb(fake))
    assert fake.queries == ['artist:"Artist A" AND (releasegroup:"Alpha")']
    state = json.loads((tmp_path / "ids.json").read_text())
    assert state["matched"] == {"a1": uid(1), "a2": uid(2)}


def test_match_catalog_uses_joined_name_for_multi_artist_release(mb, tmp_path):
    catalog = make_catalog([rel("ab", "Joint", ["a", "b"])])
    fake = FakeMusicBrainz(
        {"Artist A, Artist B": [rg(1, "Joint", credit=[credit("Artist A", " & "), credit("Artist B")])]}
    )
    match_catalog(catalog, tmp_path / "ids.json", mb(fake))
    assert json.loads((tmp_path / "ids.json").read_text())["matched"] == {"ab": uid(1)}


def test_match_catalog_truncated_response_retries_missing_title_alone(mb, tmp_path):
    catalog = make_catalog([rel("a1", "Alpha", ["a"]), rel("a2", "Beta", ["a"])])
    queries = []

    def handler(request):
        query = request.url.params["query"]
        queries.append(query)
        if "Beta" in query and "Alpha" in query:
            return httpx.Response(
                200, json={"count": 150, "release-groups": [rg(1, "Alpha", "Artist A")]}
            )
        return httpx.Response(
            200, json={"count": 1, "release-groups": [rg(2, "Beta", "Artist A")]}
        )

    report = match_catalog(catalog, tmp_path / "ids.json", mb(handler))
    assert queries[1:] == ['artist:"Artist A" AND (releasegroup:"Beta")']
    assert len(queries) == 2
    assert report["matched"] == 2
    state = json.loads((tmp_path / "ids.json").read_text())
    assert state["matched"] == {"a1": uid(1), "a2": uid(2)}


def test_match_catalog_complete_response_does_not_retry_absent_title(mb, tmp_path):
    catalog = make_catalog([rel("a1", "Alpha", ["a"]), rel("a2", "Beta", ["a"])])
    fake = FakeMusicBrainz({"Artist A": [rg(1, "Alpha", "Artist A")]})
    match_catalog(catalog, tmp_path / "ids.json", mb(fake))
    assert len(fake.queries) == 1
    assert json.loads((tmp_path / "ids.json").read_text())["unmatched"] == {"a2": "absent"}


def test_match_catalog_http_400_on_batch_retries_one_by_one(mb, tmp_path):
    catalog = make_catalog([rel("a1", "Alpha", ["a"]), rel("a2", "Beta", ["a"])])
    queries = []

    def handler(request):
        query = request.url.params["query"]
        queries.append(query)
        if " OR " in query:
            return httpx.Response(400)
        title = "Alpha" if "Alpha" in query else "Beta"
        n = 1 if title == "Alpha" else 2
        return httpx.Response(
            200, json={"count": 1, "release-groups": [rg(n, title, "Artist A")]}
        )

    report = match_catalog(catalog, tmp_path / "ids.json", mb(handler))
    assert len(queries) == 3
    assert report["matched"] == 2 and report["query_rejected"] == 0


def test_match_catalog_http_400_on_single_query_is_query_rejected(mb, tmp_path):
    catalog = make_catalog([rel("a1", "Alpha", ["a"])])
    report = match_catalog(
        catalog, tmp_path / "ids.json", mb(lambda request: httpx.Response(400))
    )
    assert report["query_rejected"] == 1 and report["phase"] == "complete"
    assert json.loads((tmp_path / "ids.json").read_text())["unmatched"] == {"a1": "query_rejected"}


@pytest.mark.parametrize(
    "failure",
    [
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("down", request=request)),
        lambda request: httpx.Response(500),
        lambda request: httpx.Response(200, json={"error": "unexpected"}),
    ],
    ids=["connect-error", "http-500", "bad-payload"],
)
def test_match_catalog_failed_batch_is_left_unrecorded_and_retried_next_run(
    mb, tmp_path, failure
):
    catalog = three_artist_catalog()
    ids_path = tmp_path / "ids.json"

    def handler(request):
        if "Artist A" in request.url.params["query"]:
            return failure(request)
        return FakeMusicBrainz({"Artist B": [rg(2, "Gamma", "Artist B")]})(request)

    report = match_catalog(catalog, ids_path, mb(handler), sleep=lambda seconds: None)
    state = json.loads(ids_path.read_text())
    assert "a1" not in state["matched"] and "a1" not in state["unmatched"]
    assert state["matched"] == {"b1": uid(2)}
    assert state["unmatched"] == {"c1": "absent"}
    assert report["remaining"] == 1

    healed = FakeMusicBrainz({"Artist A": [rg(1, "Alpha", "Artist A")]})
    again = match_catalog(catalog, ids_path, mb(healed))
    assert again["requests_this_run"] == 1 and again["remaining"] == 0
    assert json.loads(ids_path.read_text())["matched"]["a1"] == uid(1)


def test_match_catalog_five_consecutive_failures_raise_but_state_is_saved(mb, tmp_path):
    artists = [Artist(id=f"x{i}", name=f"Artist {i}") for i in range(6)]
    catalog = make_catalog([rel(f"r{i}", "Alpha", [f"x{i}"]) for i in range(6)], artists)
    ids_path = tmp_path / "ids.json"
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("down", request=request)

    pauses = []
    with pytest.raises(RuntimeError, match="five consecutive"):
        match_catalog(catalog, ids_path, mb(handler), sleep=pauses.append)
    assert len(calls) == 5
    assert pauses == [5.0, 15.0, 60.0, 300.0] == list(listenbrainz.FAILURE_PAUSES)
    state = json.loads(ids_path.read_text())
    assert state["matched"] == {} and state["unmatched"] == {}
    assert state["snapshot_id"] == "snap-1" and state["updated_at"]


def test_match_catalog_success_resets_consecutive_failure_count(mb, tmp_path):
    artists = [Artist(id=f"x{i}", name=f"Artist {i}") for i in range(9)]
    catalog = make_catalog([rel(f"r{i}", "Alpha", [f"x{i}"]) for i in range(9)], artists)

    def handler(request):
        if request.url.params["query"].startswith(('artist:"Artist 4"', 'artist:"Artist 8"')):
            return httpx.Response(200, json={"count": 0, "release-groups": []})
        raise httpx.ConnectError("down", request=request)

    # 4 failures, one success, then 3 failures and a success: never five in a row.
    pauses = []
    report = match_catalog(catalog, tmp_path / "ids.json", mb(handler), sleep=pauses.append)
    assert report["phase"] == "complete"
    assert report["absent"] == 2 and report["remaining"] == 7
    # Pauses restart from the first one after each success.
    assert pauses == [5.0, 15.0, 60.0, 300.0, 5.0, 15.0, 60.0]


def test_match_catalog_run_without_failures_never_sleeps(mb, tmp_path):
    pauses = []
    fake = FakeMusicBrainz({"Artist A": [rg(1, "Alpha", "Artist A")]})
    match_catalog(three_artist_catalog(), tmp_path / "ids.json", mb(fake), sleep=pauses.append)
    assert pauses == []


@pytest.mark.parametrize(
    "failure",
    [
        lambda request: (_ for _ in ()).throw(
            httpx.RemoteProtocolError("Server disconnected", request=request)
        ),
        lambda request: httpx.Response(503),
    ],
    ids=["transport-error", "status-error"],
)
def test_match_catalog_transport_and_status_failures_pause_alike(mb, tmp_path, failure):
    artists = [Artist(id=f"x{i}", name=f"Artist {i}") for i in range(3)]
    catalog = make_catalog([rel(f"r{i}", "Alpha", [f"x{i}"]) for i in range(3)], artists)
    pauses = []
    report = match_catalog(catalog, tmp_path / "ids.json", mb(failure), sleep=pauses.append)
    assert pauses == [5.0, 15.0, 60.0]
    assert report["phase"] == "complete" and report["remaining"] == 3


def test_load_ids_rejects_state_of_another_snapshot(tmp_path):
    catalog = make_catalog([rel("a1")])
    path = tmp_path / "ids.json"
    path.write_text(json.dumps({"snapshot_id": "other", "matched": {}, "unmatched": {}}))
    with pytest.raises(ValueError, match="snap-1"):
        load_ids(path, catalog)


def test_load_ids_rejects_malformed_state_and_creates_fresh_one(tmp_path):
    catalog = make_catalog([rel("a1")])
    path = tmp_path / "ids.json"
    path.write_text(json.dumps({"snapshot_id": "snap-1", "matched": [], "unmatched": {}}))
    with pytest.raises(ValueError):
        load_ids(path, catalog)
    fresh = load_ids(tmp_path / "absent.json", catalog)
    assert fresh["snapshot_id"] == "snap-1"
    assert fresh["matched"] == {} and fresh["unmatched"] == {}


def test_match_catalog_with_foreign_state_file_refuses_to_run(mb, tmp_path):
    path = tmp_path / "ids.json"
    path.write_text(json.dumps({"snapshot_id": "other", "matched": {}, "unmatched": {}}))
    fake = FakeMusicBrainz({})
    with pytest.raises(ValueError):
        match_catalog(make_catalog([rel("a1")]), path, mb(fake))
    assert fake.queries == []


# ---------------------------------------------------------------- release_group_ids


def test_release_group_ids_merges_matched_and_own_uuid_ids():
    catalog = make_catalog(
        [rel("a1"), rel(uid(7)), rel("junk"), rel("short"), rel("none")]
    )
    state = {"matched": {"a1": uid(1), "junk": "not-a-uuid", "short": 12, "other": uid(2)}}
    assert release_group_ids(catalog, state) == {"a1": uid(1), uid(7): uid(7)}


# ---------------------------------------------------------------- ListenBrainzClient


def lb_client(handler, sleeps=None):
    sleeps = [] if sleeps is None else sleeps
    return ListenBrainzClient(transport=httpx.MockTransport(handler), sleep=sleeps.append), sleeps


def counts(mbids_counts):
    return [
        {"release_group_mbid": m, "total_listen_count": c * 3, "total_user_count": c}
        for m, c in mbids_counts
    ]


def test_release_group_listeners_posts_batch_and_maps_by_mbid_not_order():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.host == "api.listenbrainz.org"
        return httpx.Response(200, json=counts([(uid(2), 20), (uid(1), 10)]))

    client, sleeps = lb_client(handler)
    result = client.release_group_listeners([uid(1), uid(2)])
    assert result == {uid(1): 10, uid(2): 20}
    assert len(seen) == 1 and seen[0].method == "POST"
    assert seen[0].url.path == "/1/popularity/release-group"
    assert json.loads(seen[0].content) == {"release_group_mbids": [uid(1), uid(2)]}
    assert client.network_requests == 1 and sleeps == []
    client.close()


def test_release_group_listeners_drops_unknown_values():
    rows = [
        {"release_group_mbid": uid(1), "total_user_count": None},
        {"release_group_mbid": uid(2), "total_user_count": 0},
        {"release_group_mbid": uid(3), "total_user_count": "5"},
        {"release_group_mbid": uid(4), "total_user_count": True},
        {"release_group_mbid": uid(5), "total_user_count": 5.0},
        {"release_group_mbid": uid(6), "total_user_count": 6},
        {"release_group_mbid": uid(99), "total_user_count": 99},
        {"total_user_count": 7},
        None,
        "text",
        {"release_group_mbid": uid(7), "total_user_count": 7},
    ]
    client, _ = lb_client(lambda request: httpx.Response(200, json=rows))
    assert client.release_group_listeners([uid(n) for n in range(1, 8)]) == {uid(6): 6, uid(7): 7}


def test_release_group_listeners_waits_reset_time_on_429_and_retries():
    responses = [
        httpx.Response(429, headers={"X-RateLimit-Reset-In": "3"}),
        httpx.Response(200, json=counts([(uid(1), 4)])),
    ]
    client, sleeps = lb_client(lambda request: responses.pop(0))
    assert client.release_group_listeners([uid(1)]) == {uid(1): 4}
    assert sleeps == [3.0]
    assert client.network_requests == 2


def test_release_group_listeners_backs_off_without_reset_header_then_gives_up():
    client, sleeps = lb_client(lambda request: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError) as info:
        client.release_group_listeners([uid(1)])
    assert info.value.response.status_code == 503
    assert client.network_requests == 4
    assert sleeps == [2.0, 4.0, 6.0]


def test_release_group_listeners_does_not_retry_client_errors():
    client, sleeps = lb_client(lambda request: httpx.Response(400))
    with pytest.raises(httpx.HTTPStatusError):
        client.release_group_listeners([uid(1)])
    assert client.network_requests == 1 and sleeps == []


def test_release_group_listeners_pauses_when_quota_is_exhausted():
    headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-In": "7"}
    client, sleeps = lb_client(
        lambda request: httpx.Response(200, json=counts([(uid(1), 4)]), headers=headers)
    )
    assert client.release_group_listeners([uid(1)]) == {uid(1): 4}
    assert sleeps == [7.0]


def test_release_group_listeners_does_not_pause_with_quota_left():
    headers = {"X-RateLimit-Remaining": "5", "X-RateLimit-Reset-In": "7"}
    client, sleeps = lb_client(lambda request: httpx.Response(200, json=[], headers=headers))
    assert client.release_group_listeners([uid(1)]) == {}
    assert sleeps == []


def test_release_group_listeners_rejects_bad_batch_sizes_without_request():
    calls = []
    client, _ = lb_client(lambda request: calls.append(request))
    with pytest.raises(ValueError):
        client.release_group_listeners([])
    with pytest.raises(ValueError):
        client.release_group_listeners([uid(n) for n in range(501)])
    assert calls == [] and client.network_requests == 0


def test_release_group_listeners_accepts_exactly_500_ids():
    client, _ = lb_client(lambda request: httpx.Response(200, json=[]))
    assert client.release_group_listeners([uid(n) for n in range(500)]) == {}


def test_release_group_listeners_rejects_non_list_response():
    client, _ = lb_client(lambda request: httpx.Response(200, json={"error": "x"}))
    with pytest.raises(ValueError, match="unexpected"):
        client.release_group_listeners([uid(1)])


# ---------------------------------------------------------------- collect_listeners


class FakeListenBrainz:
    def __init__(self, users):
        self.users = users
        self.batches = []

    def __call__(self, request):
        mbids = json.loads(request.content)["release_group_mbids"]
        self.batches.append(mbids)
        rows = [
            {"release_group_mbid": m, "total_listen_count": 99, "total_user_count": self.users[m]}
            for m in reversed(mbids)
            if m in self.users
        ]
        return httpx.Response(200, json=rows)


def test_collect_listeners_dedupes_mbids_and_builds_overlay():
    catalog = make_catalog(
        [rel("a1"), rel("a2", "Alpha (deluxe)"), rel("a3", "Third"), rel(uid(5), "Own")]
    )
    state = {"matched": {"a1": uid(1), "a2": uid(1), "a3": uid(3)}}
    fake = FakeListenBrainz({uid(1): 12, uid(5): 8})
    client, _ = lb_client(fake)
    overlay = collect_listeners(catalog, state, client)
    assert fake.batches == [sorted([uid(1), uid(3), uid(5)])]
    assert overlay["listeners"] == {"a1": 12, "a2": 12, uid(5): 8}
    assert overlay["snapshot_id"] == "snap-1"
    assert overlay["release_groups_requested"] == 3
    assert datetime.fromisoformat(overlay["observed_at"]).tzinfo is not None


def test_collect_listeners_splits_into_batches_and_reports_progress(monkeypatch):
    monkeypatch.setattr(listenbrainz, "MBIDS_PER_REQUEST", 2)
    catalog = make_catalog([rel(f"r{i}", f"T{i}") for i in range(5)])
    state = {"matched": {f"r{i}": uid(i + 1) for i in range(5)}}
    fake = FakeListenBrainz({uid(i + 1): i + 1 for i in range(5)})
    client, _ = lb_client(fake)
    progress = []
    overlay = collect_listeners(catalog, state, client, progress=progress.append)
    assert [len(b) for b in fake.batches] == [2, 2, 1]
    assert client.network_requests == 3
    assert overlay["listeners"] == {f"r{i}": i + 1 for i in range(5)}
    assert [p["requested"] for p in progress] == [2, 4, 5]
    assert progress[-1]["with_listeners"] == 5 and progress[-1]["total"] == 5


def test_collect_listeners_omits_releases_without_data():
    catalog = make_catalog([rel("a1"), rel("a2", "Beta"), rel("a3", "Unmatched")])
    state = {"matched": {"a1": uid(1), "a2": uid(2)}}
    client, _ = lb_client(FakeListenBrainz({uid(1): 3}))
    overlay = collect_listeners(catalog, state, client)
    assert overlay["listeners"] == {"a1": 3}
    assert overlay["release_groups_requested"] == 2


# ---------------------------------------------------------------- apply_listeners


OBSERVED = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def overlay_for(catalog, listeners):
    return {
        "snapshot_id": catalog.snapshot_id,
        "observed_at": OBSERVED.isoformat(),
        "listeners": listeners,
    }


def old_observation():
    return Observation(
        source="musicbrainz",
        observed_at=datetime(2026, 10, 1, tzinfo=UTC),
        rating=4.0,
        rating_scale=5,
        rating_count=10,
        access_basis="fixture",
    )


def test_apply_listeners_adds_one_observation_and_keeps_existing_ones():
    catalog = make_catalog([rel("a1", observations=[old_observation()]), rel("a2", "Beta")])
    result = apply_listeners(catalog, overlay_for(catalog, {"a1": 42}))
    first, second = result.releases
    assert [o.source for o in first.observations] == ["musicbrainz", "listenbrainz"]
    added = first.observations[-1]
    assert added.rating_count == 42 and added.rating is None and added.url == ""
    assert added.observed_at == OBSERVED
    assert added.access_basis
    assert first.observations[0] == old_observation()
    assert second.observations == [] and second == catalog.releases[1]


def test_apply_listeners_does_not_mutate_source_catalog():
    catalog = make_catalog([rel("a1", observations=[old_observation()])])
    before = catalog.model_dump()
    result = apply_listeners(catalog, overlay_for(catalog, {"a1": 42}))
    assert catalog.model_dump() == before
    assert result is not catalog
    assert len(catalog.releases[0].observations) == 1


def test_apply_listeners_rejects_overlay_of_another_snapshot():
    catalog = make_catalog([rel("a1")])
    overlay = overlay_for(catalog, {"a1": 1}) | {"snapshot_id": "other"}
    with pytest.raises(ValueError, match="another catalogue snapshot"):
        apply_listeners(catalog, overlay)


# ---------------------------------------------------------------- load_with_listeners


def test_load_with_listeners_without_overlay_equals_load_catalog(tmp_path):
    from discovery.catalog import load_catalog

    catalog = make_catalog([rel("a1")])
    path = tmp_path / "catalog.json"
    save_catalog(catalog, path)
    assert load_with_listeners(path) == load_catalog(path)
    assert load_with_listeners(str(path)).releases[0].observations == []


def test_load_with_listeners_applies_overlay_next_to_catalog(tmp_path):
    catalog = make_catalog([rel("a1"), rel("a2", "Beta")])
    path = tmp_path / "catalog.json"
    save_catalog(catalog, path)
    (tmp_path / "listenbrainz.json").write_text(json.dumps(overlay_for(catalog, {"a1": 5})))
    loaded = load_with_listeners(path)
    assert [o.source for o in loaded.releases[0].observations] == ["listenbrainz"]
    assert loaded.releases[0].observations[0].rating_count == 5
    assert loaded.releases[1].observations == []


def test_load_with_listeners_warns_and_ignores_overlay_of_another_snapshot(tmp_path):
    catalog = make_catalog([rel("a1")])
    path = tmp_path / "catalog.json"
    save_catalog(catalog, path)
    stale = overlay_for(catalog, {"a1": 5}) | {"snapshot_id": "old"}
    (tmp_path / "listenbrainz.json").write_text(json.dumps(stale))
    with pytest.warns(UserWarning, match="another catalogue snapshot"):
        loaded = load_with_listeners(path)
    assert loaded.releases[0].observations == []


# ---------------------------------------------------------------- status


def test_status_without_overlay_counts_matching_progress(tmp_path):
    catalog = make_catalog(
        [rel("a1"), rel("a2", "Beta"), rel("a3", "Gamma"), rel(uid(8), "Own"), rel("a5", "Five")]
    )
    ids_path = tmp_path / "ids.json"
    ids_path.write_text(
        json.dumps(
            {
                "snapshot_id": "snap-1",
                "method": MATCH_METHOD,
                "matched": {"a1": uid(1)},
                "unmatched": {"a2": "absent"},
            }
        )
    )
    report = status(catalog, ids_path, tmp_path / "listenbrainz.json")
    assert report == {
        "snapshot_id": "snap-1",
        "releases": 5,
        "with_release_group_id": 2,
        "unmatched": 1,
        "not_searched_yet": 2,
        "listeners_collected": False,
    }


def test_status_with_overlay_reports_age_and_counts(tmp_path):
    catalog = make_catalog([rel("a1"), rel("a2", "Beta")])
    observed = datetime.now(UTC) - timedelta(days=5, hours=1)
    overlay_path = tmp_path / "listenbrainz.json"
    overlay_path.write_text(
        json.dumps(
            {
                "snapshot_id": "snap-1",
                "observed_at": observed.isoformat(timespec="seconds"),
                "listeners": {"a1": 3},
            }
        )
    )
    report = status(catalog, tmp_path / "ids.json", overlay_path)
    assert report["listeners_collected"] is True
    assert report["releases_with_listeners"] == 1
    assert report["age_days"] == 5
    assert report["overlay_matches_snapshot"] is True
    assert report["not_searched_yet"] == 2
    assert not (tmp_path / "ids.json").exists()


# ---------------------------------------------------------------- ranking integration


def test_ranker_uses_listenbrainz_listeners_as_popularity(tmp_path):
    catalog = make_catalog([rel("a1", year=2020)])
    state = {"matched": {"a1": uid(1)}}
    client, _ = lb_client(FakeListenBrainz({uid(1): 77}))
    overlay = collect_listeners(catalog, state, client)
    enriched = apply_listeners(catalog, overlay)
    as_of = datetime.fromisoformat(overlay["observed_at"]).date()
    result = score_candidates(
        enriched, RequestIntent(genre_scope_id="hip"), UserProfile(), as_of
    )
    components = {item.release_id: item.components for item in result.items}["a1"]
    assert components["popularity_source"] == "listenbrainz"
    assert components["rating_count"] == 77


def test_ranker_without_listenbrainz_data_has_no_popularity_source():
    catalog = make_catalog([rel("a1", year=2020)])
    result = score_candidates(
        catalog, RequestIntent(genre_scope_id="hip"), UserProfile(), date(2026, 11, 1)
    )
    assert result.items[0].components["popularity_source"] is None


# ---------------------------------------------------------------- no network on import


def test_importing_module_makes_no_network_requests():
    code = (
        "import socket\n"
        "def boom(*a, **k):\n"
        "    raise AssertionError('network access at import')\n"
        "socket.socket.connect = boom\n"
        "socket.getaddrinfo = boom\n"
        "import discovery.catalog.listenbrainz as m\n"
        "m.ListenBrainzClient().close()\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_bracketed_transliteration_matches_either_part_and_queries_the_first():
    mbid = "11111111-2222-4333-8444-555555555555"
    rows = [{"id": mbid, "title": "Duty", "primary-type": "Album",
             "artist-credit": [{"name": "浜崎あゆみ", "artist": {"name": "浜崎あゆみ"}}]}]
    name = "浜崎あゆみ [Ayumi Hamasaki]"

    assert listenbrainz.artist_names(name) == [name, "浜崎あゆみ", "Ayumi Hamasaki"]
    assert listenbrainz.artist_names("Russ") == ["Russ"]
    assert listenbrainz.choose_release_group("Duty", name, 2000, rows) == (mbid, "matched")
    latin = [dict(rows[0], **{"artist-credit": [{"name": "Ayumi Hamasaki"}]})]
    assert listenbrainz.choose_release_group("Duty", name, None, latin) == (mbid, "matched")
    assert listenbrainz.choose_release_group("Duty", "Someone [Else]", None, rows) == (None, "absent")


# ---------------------------------------------------------------- state of another method


def test_load_ids_rejects_state_written_by_another_method(tmp_path):
    catalog = make_catalog([rel("a1")])
    path = tmp_path / "ids.json"
    path.write_text(
        json.dumps(
            {"snapshot_id": "snap-1", "method": "old-method", "matched": {}, "unmatched": {}}
        )
    )
    with pytest.raises(ValueError, match="method"):
        load_ids(path, catalog)
    path.write_text(json.dumps({"snapshot_id": "snap-1", "matched": {}, "unmatched": {}}))
    with pytest.raises(ValueError, match="method"):
        load_ids(path, catalog)


def test_load_ids_accepts_state_of_current_method(tmp_path):
    catalog = make_catalog([rel("a1")])
    path = tmp_path / "ids.json"
    path.write_text(
        json.dumps(
            {"snapshot_id": "snap-1", "method": MATCH_METHOD, "matched": {"a1": uid(1)},
             "unmatched": {}}
        )
    )
    assert load_ids(path, catalog)["matched"] == {"a1": uid(1)}
    assert MATCH_METHOD.endswith("-year-v2")


def test_match_catalog_with_other_method_state_makes_no_requests(mb, tmp_path):
    path = tmp_path / "ids.json"
    path.write_text(
        json.dumps({"snapshot_id": "snap-1", "method": "v1", "matched": {}, "unmatched": {}})
    )
    fake = FakeMusicBrainz({})
    with pytest.raises(ValueError):
        match_catalog(make_catalog([rel("a1")]), path, mb(fake))
    assert fake.queries == []


# ---------------------------------------------------------------- skipped releases


def test_match_catalog_skips_blank_titles_and_releases_without_artists(mb, tmp_path):
    catalog = make_catalog([rel("a1", "Alpha"), rel("blank", "Beta"), rel("noartist", "Gamma")])
    releases = [
        catalog.releases[0],
        catalog.releases[1].model_copy(update={"title": "   "}),
        catalog.releases[2].model_copy(update={"artist_ids": []}),
    ]
    catalog = catalog.model_copy(update={"releases": releases})
    fake = FakeMusicBrainz({"Artist A": [rg(1, "Alpha", "Artist A")]})
    report = match_catalog(catalog, tmp_path / "ids.json", mb(fake))
    assert fake.queries == ['artist:"Artist A" AND (releasegroup:"Alpha")']
    state = json.loads((tmp_path / "ids.json").read_text())
    assert state["matched"] == {"a1": uid(1)} and state["unmatched"] == {}
    assert report["remaining"] == 0


# ---------------------------------------------------------------- interruption


def test_match_catalog_interrupt_keeps_earlier_results_and_resumes(mb, tmp_path):
    catalog = three_artist_catalog()
    ids_path = tmp_path / "ids.json"
    db = {
        "Artist A": [rg(1, "Alpha", "Artist A")],
        "Artist B": [rg(2, "Gamma", "Artist B")],
        "Artist C": [rg(3, "Delta", "Artist C")],
    }
    fake = FakeMusicBrainz(db)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 3:
            raise KeyboardInterrupt
        return fake(request)

    client = mb(handler)
    with pytest.raises(KeyboardInterrupt):
        match_catalog(catalog, ids_path, client)
    state = json.loads(ids_path.read_text())
    assert state["matched"] == {"a1": uid(1), "b1": uid(2)}
    assert state["unmatched"] == {}
    assert "c1" not in state["matched"] and "c1" not in state["unmatched"]

    resumed = FakeMusicBrainz(db)
    again = match_catalog(catalog, ids_path, mb(resumed))
    assert resumed.queries == ['artist:"Artist C" AND (releasegroup:"Delta")']
    assert again["phase"] == "complete"
    assert json.loads(ids_path.read_text())["matched"] == {
        "a1": uid(1),
        "b1": uid(2),
        "c1": uid(3),
    }


# ---------------------------------------------------------------- transport errors


def test_release_group_listeners_retries_transport_errors_then_succeeds():
    outcomes = [httpx.ReadTimeout("slow"), httpx.ConnectError("down"), None]

    def handler(request):
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        return httpx.Response(200, json=counts([(uid(1), 4)]))

    client, sleeps = lb_client(handler)
    assert client.release_group_listeners([uid(1)]) == {uid(1): 4}
    assert sleeps == [2.0, 4.0]
    assert client.network_requests == 3


def test_release_group_listeners_gives_up_on_persistent_transport_error():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    client, sleeps = lb_client(handler)
    with pytest.raises(httpx.ConnectError):
        client.release_group_listeners([uid(1)])
    assert client.network_requests == 4
    assert sleeps == [2.0, 4.0, 6.0]


# ---------------------------------------------------------------- overlay robustness


def write_catalog(tmp_path, releases=None):
    catalog = make_catalog(releases or [rel("a1"), rel("a2", "Beta")])
    path = tmp_path / "catalog.json"
    save_catalog(catalog, path)
    return catalog, path


def test_load_with_listeners_survives_broken_json(tmp_path):
    catalog, path = write_catalog(tmp_path)
    (tmp_path / "listenbrainz.json").write_text("{not json")
    with pytest.warns(UserWarning, match="listenbrainz.json"):
        loaded = load_with_listeners(path)
    assert loaded == catalog


@pytest.mark.parametrize(
    "damage",
    [
        lambda o: o.pop("observed_at"),
        lambda o: o.pop("listeners"),
        lambda o: o.update(observed_at="not a date"),
        lambda o: o.update(observed_at="2026-11-01T12:00:00"),
        lambda o: o.update(observed_at=None),
        lambda o: o.update(listeners=[1, 2]),
        lambda o: o.update(listeners=None),
    ],
    ids=["no-observed-at", "no-listeners", "bad-date", "naive-date", "null-date", "list", "null"],
)
def test_load_with_listeners_survives_incomplete_overlay(tmp_path, damage):
    catalog, path = write_catalog(tmp_path)
    overlay = overlay_for(catalog, {"a1": 5})
    damage(overlay)
    (tmp_path / "listenbrainz.json").write_text(json.dumps(overlay))
    with pytest.warns(UserWarning, match="listenbrainz.json"):
        loaded = load_with_listeners(path)
    assert loaded == catalog


def test_load_with_listeners_overlay_that_is_not_an_object_is_ignored(tmp_path):
    catalog, path = write_catalog(tmp_path)
    (tmp_path / "listenbrainz.json").write_text("[1, 2]")
    with pytest.warns(UserWarning):
        assert load_with_listeners(path) == catalog


def test_apply_listeners_accepts_only_positive_integers():
    catalog = make_catalog([rel(f"r{i}", f"T{i}") for i in range(7)])
    values = {"r0": -3, "r1": "5", "r2": True, "r3": 0, "r4": 2.5, "r5": None, "r6": 9}
    result = apply_listeners(catalog, overlay_for(catalog, values))
    counted = {r.id: [o.rating_count for o in r.observations] for r in result.releases}
    assert counted == {"r0": [], "r1": [], "r2": [], "r3": [], "r4": [], "r5": [], "r6": [9]}


def test_load_with_listeners_stale_overlay_is_applied_with_a_warning(tmp_path):
    catalog, path = write_catalog(tmp_path)
    old = datetime.now(UTC) - timedelta(days=listenbrainz.MAX_AGE_DAYS + 10)
    overlay = overlay_for(catalog, {"a1": 5}) | {"observed_at": old.isoformat(timespec="seconds")}
    (tmp_path / "listenbrainz.json").write_text(json.dumps(overlay))
    with pytest.warns(UserWarning, match="listenbrainz listeners"):
        loaded = load_with_listeners(path)
    assert loaded.releases[0].observations[0].rating_count == 5


def test_load_with_listeners_fresh_overlay_does_not_warn(tmp_path, recwarn):
    catalog, path = write_catalog(tmp_path)
    fresh = datetime.now(UTC) - timedelta(days=3)
    overlay = overlay_for(catalog, {"a1": 5}) | {"observed_at": fresh.isoformat(timespec="seconds")}
    (tmp_path / "listenbrainz.json").write_text(json.dumps(overlay))
    load_with_listeners(path)
    assert [w for w in recwarn if issubclass(w.category, UserWarning)] == []


def test_max_age_matches_ranker_window():
    assert listenbrainz.MAX_AGE_DAYS == 90


def test_status_marks_old_overlay_stale_and_fresh_one_not(tmp_path):
    catalog = make_catalog([rel("a1")])
    overlay_path = tmp_path / "listenbrainz.json"
    for days, expected in ((5, False), (120, True)):
        observed = datetime.now(UTC) - timedelta(days=days, hours=1)
        overlay_path.write_text(
            json.dumps(
                {
                    "snapshot_id": "snap-1",
                    "observed_at": observed.isoformat(timespec="seconds"),
                    "listeners": {"a1": 3},
                }
            )
        )
        assert status(catalog, tmp_path / "ids.json", overlay_path)["stale"] is expected


@pytest.mark.parametrize("text", ["{broken", "[]", '{"snapshot_id": "snap-1"}'])
def test_status_reports_broken_overlay_instead_of_raising(tmp_path, text):
    catalog = make_catalog([rel("a1")])
    overlay_path = tmp_path / "listenbrainz.json"
    overlay_path.write_text(text)
    report = status(catalog, tmp_path / "ids.json", overlay_path)
    assert report["overlay_error"]
    assert report["listeners_collected"] is False


# ---------------------------------------------------------------- main


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """Runs ``main`` with a MockTransport MusicBrainz client and an isolated lock file."""
    monkeypatch.setattr(musicbrainz.time, "sleep", lambda *_: None)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    saved = {n: signal.getsignal(n) for n in (signal.SIGTERM, signal.SIGHUP)}
    catalog = three_artist_catalog()
    catalog_path = tmp_path / "catalog.json"
    save_catalog(catalog, catalog_path)
    ids_path = tmp_path / "ids.json"
    clients = []

    def run(handler):
        def factory():
            client = MusicBrainzClient(transport=httpx.MockTransport(handler))
            clients.append(client)
            return client

        monkeypatch.setattr(listenbrainz, "MusicBrainzClient", factory)
        monkeypatch.setattr(
            sys, "argv", ["listenbrainz", "match", "--catalog", str(catalog_path),
                          "--ids", str(ids_path)]
        )
        return listenbrainz.main()

    yield run, ids_path
    for number, handler in saved.items():
        signal.signal(number, handler)





def test_main_match_returns_zero_and_writes_state(cli, capsys):
    run, ids_path = cli
    fake = FakeMusicBrainz({"Artist A": [rg(1, "Alpha", "Artist A")]})
    assert run(fake) == 0
    assert json.loads(ids_path.read_text())["matched"] == {"a1": uid(1)}
    assert '"phase": "complete"' in capsys.readouterr().out


def test_main_match_returns_130_on_keyboard_interrupt_and_keeps_progress(cli, capsys):
    run, ids_path = cli
    fake = FakeMusicBrainz({"Artist A": [rg(1, "Alpha", "Artist A")]})
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return fake(request)

    assert run(handler) == 130
    assert json.loads(ids_path.read_text())["matched"] == {"a1": uid(1)}
    assert "interrupted" in capsys.readouterr().out


def test_main_match_installs_sigterm_and_sighup_handlers(cli):
    run, _ = cli
    assert run(FakeMusicBrainz({})) == 0
    assert signal.getsignal(signal.SIGTERM) is listenbrainz._interrupt
    assert signal.getsignal(signal.SIGHUP) is listenbrainz._interrupt


def test_main_match_exits_when_another_run_holds_the_lock(cli, tmp_path):
    run, ids_path = cli
    calls = []
    with (tmp_path / "music-discovery-musicbrainz.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit, match="already active"):
            run(lambda request: calls.append(request))
    assert calls == [] and not ids_path.exists()


def test_module_does_not_import_fcntl_at_load_time():
    assert not hasattr(listenbrainz, "fcntl")
