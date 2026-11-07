"""User-authored musical evals. Gold answers never enter the chat request.

Name matches are diagnostics, not cross-source identity resolution. Positive
artist examples are non-exhaustive; no automatic musical-usefulness pass.
"""

import re
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import urlsplit

from discovery.contracts import Catalog, UserProfile
from discovery.llm import BudgetLedger, LLMError
from discovery.storage import Store
from discovery.web.chat import create_chat, get_chat, handle_message, update_filters
from discovery.web.schemas import ChatCreate, Filters, Message, MessageRequest


def name_key(name: str) -> str:
    return re.sub(r"[^\w]", "", name.casefold())


def eligible_names(dataset: dict) -> set[str]:
    domains = {"aoty": "www.albumoftheyear.org", "rym": "rateyourmusic.com"}
    return {name_key(ref["name"]) for ref in dataset.get("artist_references", [])
            if ref.get("status") == "verified"
            and ref.get("source") in dataset["artist_source_policy"]["required_any_of"]
            and urlsplit(ref["url"]).hostname == domains.get(ref["source"])}


def catalog_coverage(dataset: dict, catalog: Catalog) -> dict:
    index = {}
    for artist in catalog.artists:
        index.setdefault(name_key(artist.name), []).append(artist.id)
    eligible = eligible_names(dataset)
    rows = []
    for case in dataset["cases"]:
        examples = {}
        for polarity in ("positive", "negative"):
            examples[polarity] = [
                {"name": name, "eligible": name_key(name) in eligible,
                 "catalog_name_match_ids": index.get(name_key(name), [])}
                for name in case[f"{polarity}_artists"]]
        rows.append({"id": case["id"], "examples": examples})
    rage = catalog.descendant_ids("rage") if any(g.id == "rage" for g in catalog.genres) else set()
    return {"cases": rows, "releases": len(catalog.releases), "artists": len(catalog.artists),
            "rage_release_count": sum(bool(set(r.genre_ids) & rage) for r in catalog.releases),
            "match_method": "exact name ignoring case/punctuation; not verified identity mapping"}


def grade_case(dataset: dict, case: dict, catalog: Catalog, result: Message,
               filters: Filters, *, fame_labels: dict | None = None) -> dict:
    """Check grounded facts. Empty/failed/incomplete results cannot pass vacuously."""
    releases = {r.id: r for r in catalog.releases}
    artists = {a.id: a.name for a in catalog.artists}
    genres = {g.id: g.name for g in catalog.genres}
    cards = result.cards
    expected = case["expectations"]
    source_names = eligible_names(dataset)
    positives = {name_key(n) for n in case["positive_artists"]} & source_names
    negatives = {name_key(n) for n in case["negative_artists"]} & source_names
    names = {name_key(n) for card in cards for n in card.artists}
    grounded = bool(cards) and all(
        card.release_id in releases
        and card.title == releases[card.release_id].title
        and card.artists == [artists[a] for a in releases[card.release_id].artist_ids]
        and card.year == releases[card.release_id].year
        and card.genres == [genres[g] for g in releases[card.release_id].genre_ids]
        for card in cards)
    checks = {
        "completed": result.status == "completed",
        "five_results": len(cards) == expected["count"],
        "grounded": grounded,
        "no_duplicate_releases": bool(cards) and len({c.release_id for c in cards}) == len(cards),
        "genre_preserved": filters.genre_scope_id == expected["genre_scope_id"],
        "no_negative_examples": bool(cards) and not names & negatives,
    }
    for label, genre_id in (("cards_in_scope", expected["genre_scope_id"]),
                            ("required_release_genre", expected.get("required_release_genre_id"))):
        if genre_id:
            allowed = catalog.descendant_ids(genre_id) if genre_id in genres else set()
            checks[label] = bool(cards) and all(c.release_id in releases and
                bool(set(releases[c.release_id].genre_ids) & allowed) for c in cards)
    if expected.get("mode"):
        checks["requested_mode"] = filters.mode == expected["mode"]
    labels = {name_key(n): value for n, value in (fame_labels or {}).items()}
    if any(type(value) is not bool for value in labels.values()):
        raise ValueError("Fame labels must be explicit true/false; unknown names must be omitted")
    famous = sum(any(labels.get(name_key(n)) is True for n in c.artists) for c in cards)
    unknown = [c.release_id for c in cards if not any(labels.get(name_key(n)) is True
                for n in c.artists) and any(name_key(n) not in labels for n in c.artists)]
    fame = ("fail" if famous > 2 else "pass" if len(cards) == 5 and not unknown else "needs_labels")
    unverified = [n for n in case["positive_artists"] + case["negative_artists"]
                  if name_key(n) not in source_names]
    structural = all(checks.values())
    return {"checks": checks, "structural_passed": structural,
            "positive_example_matches": sorted(names & positives),
            "negative_example_matches": sorted(names & negatives),
            "unverified_examples_not_graded": unverified,
            "fame_mix": {"status": fame, "known_famous_options": famous,
                         "unlabelled_release_ids": unknown},
            "musical_relevance": "needs_human_review",
            "status": "failed" if not structural or fame == "fail" else "needs_human_review",
            "passed": False}


class EvaluationLedger(BudgetLedger):
    """Sequential run cap in addition to the application's persistent daily cap."""

    def __init__(self, path: Path, limit: float = .01):
        super().__init__(path)
        if not 0 < limit <= .01:
            raise ValueError("Live evaluation limit must be at most $0.01")
        self.limit = limit
        self.reservations: dict[str, float] = {}

    @property
    def run_spend(self) -> float:
        return sum(self.reservations.values())

    def reserve(self, amount, daily_limit, *account):
        if self.run_spend + amount > self.limit + 1e-12:
            raise LLMError("EVAL_BUDGET", "Достигнут лимит проверки $0.01.")
        ident = super().reserve(amount, min(daily_limit, .10), *account)
        self.reservations[ident] = amount
        return ident

    def settle(self, reservation, actual):
        super().settle(reservation, actual)
        self.reservations[reservation] = actual


def run_case(dataset: dict, case: dict, catalog: Catalog, client_factory, *, artifact=None) -> dict:
    """Exercise production chat in an isolated DB, including explicit UI confirmation."""
    with TemporaryDirectory(prefix="discovery-user-eval-") as directory:
        store = Store(Path(directory) / "user.sqlite3")
        listener_keys = {name_key(n) for n in case["listener_artist_names"]}
        seeds = [a.id for a in catalog.artists if name_key(a.name) in listener_keys]
        missing = [n for n in case["listener_artist_names"] if not any(
            name_key(a.name) == name_key(n) for a in catalog.artists)]
        store.set_base_profile(UserProfile(seed_artist_ids_by_genre={case.get("genre_scope_id") or "hip-hop": seeds}))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(catalog=catalog)))
        chat = create_chat(ChatCreate(filters=Filters()), request, store)
        body = MessageRequest(message_id="eval-" + case["id"],
                              text=f"Контекст слушателя: {case['context']}\nЗапрос: {case['request']}")
        result = handle_message(store, chat.id, body, catalog, client_factory, artifact)

        def unexpected_call():
            raise AssertionError("Replay must not invoke provider")

        replay = handle_message(store, chat.id, body, catalog, unexpected_call)
        idempotent = replay == result
        messages = [result.model_dump(mode="json")]
        confirmed = result.status == "completed" and result.proposed_filters is not None
        if confirmed:
            update_filters(chat.id, result.proposed_filters, request, store)
            result = handle_message(store, chat.id, MessageRequest(
                message_id="confirm-" + case["id"], apply_filters=True), catalog, artifact=artifact)
            messages.append(result.model_dump(mode="json"))
        filters = get_chat(store, chat.id).filters
        grading = grade_case(dataset, case, catalog, result, filters)
        grading["checks"]["idempotency"] = idempotent
        grading["checks"]["no_feedback_created"] = not store.feedback()
        grading["structural_passed"] = all(grading["checks"].values())
        if not grading["structural_passed"]:
            grading["status"] = "failed"
        return {"id": case["id"], "input": body.text, "missing_listener_artists": missing,
                "test_confirmed_proposal": confirmed, "messages": messages,
                "filters": filters.model_dump(), "grading": grading}
