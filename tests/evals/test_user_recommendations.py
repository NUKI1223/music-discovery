import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from discovery.config import PROJECT_ROOT
from discovery.contracts import Artist, Catalog, Genre, Release
from discovery.evals.user_recommendations import (
    EvaluationLedger,
    catalog_coverage,
    eligible_names,
    grade_case,
    run_case,
)
from discovery.evals.user_run import markdown_report
from discovery.llm import BudgetLedger, LLMError, OpenRouterClient, OpenRouterSettings
from discovery.web.schemas import Filters, Message, ReleaseCard


@pytest.fixture
def sample():
    catalog = Catalog(snapshot_id="test", is_fixture=True,
        genres=[Genre(id="hip-hop", name="Hip Hop"),
                Genre(id="rage", name="Rage", parent_ids=["hip-hop"])],
        artists=[Artist(id=f"a{i}", name=f"Artist {i}", genre_ids=["rage"]) for i in range(6)],
        releases=[Release(id=f"r{i}", title=f"Release {i}", artist_ids=[f"a{i}"],
                          genre_ids=["rage"]) for i in range(6)])
    case = {"id": "rage-example", "request": "Хочу рейдж", "context": "Слушаю Artist 0",
            "listener_artist_names": ["Artist 0"], "positive_artists": ["Secret positive"],
            "negative_artists": ["Secret negative"], "positive_rationale": "SECRET GOLD RATIONALE",
            "expectations": {"count": 5, "genre_scope_id": "hip-hop",
                             "required_release_genre_id": "rage", "mode": "surprise"}}
    dataset = {"cases": [case], "artist_source_policy": {"required_any_of": ["aoty", "rym"]},
               "artist_references": [{"name": n, "source": "aoty", "status": "verified",
                   "url": "https://www.albumoftheyear.org/artist/test/"}
                   for n in ["Secret positive", "Secret negative"]]}
    cards = [ReleaseCard(release_id=r.id, decision_id="d", title=r.title,
             artists=[f"Artist {i}"], year=None, genres=["Rage"], explanation=[],
             warnings=[], links=[], historical_ratings=[]) for i, r in enumerate(catalog.releases[:5])]
    result = Message(id="answer", role="assistant", text="Подборка",
                     created_at=datetime.now(UTC).isoformat(), cards=cards)
    return SimpleNamespace(catalog=catalog, dataset=dataset, case=case, result=result,
                           filters=Filters(mode="surprise", locked_subgenre_ids=["rage"]))


def grade(s, **kwargs):
    return grade_case(s.dataset, s.case, s.catalog, s.result, s.filters, **kwargs)


def test_positive_examples_nonexhaustive_and_unknown_fame_not_pass(sample):
    result = grade(sample)
    assert result["structural_passed"]
    assert result["positive_example_matches"] == []
    assert result["fame_mix"]["status"] == "needs_labels"
    assert not result["passed"]
    assert result["musical_relevance"] == "needs_human_review"


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted", "pending"])
def test_no_vacuous_pass_for_empty_or_failed_reply(sample, status):
    sample.result.cards = []
    sample.result.status = status
    result = grade(sample)
    assert not result["structural_passed"]
    assert result["status"] == "failed"


def test_fame_labels_and_collaboration_count_options_once(sample):
    assert grade(sample, fame_labels={f"Artist {i}": i < 2 for i in range(5)})["fame_mix"]["status"] == "pass"
    assert grade(sample, fame_labels={f"Artist {i}": True for i in range(3)})["fame_mix"]["status"] == "fail"
    sample.result.cards[0].artists += ["Unknown collaborator"]
    assert grade(sample, fame_labels={"Artist 0": True})["fame_mix"]["known_famous_options"] == 1
    with pytest.raises(ValueError):
        grade(sample, fame_labels={"Artist 0": "unknown"})


def test_source_policy_and_request_specific_negative(sample):
    sample.dataset["artist_references"].append({"name": "Artist 0", "source": "aoty",
        "status": "verified", "url": "https://www.albumoftheyear.org/artist/test/"})
    assert grade(sample)["checks"]["no_negative_examples"]
    sample.case["negative_artists"].append("Artist 0")
    assert not grade(sample)["checks"]["no_negative_examples"]
    sample.dataset["artist_references"][-1]["url"] = "https://music.apple.com/artist/0"
    assert grade(sample)["checks"]["no_negative_examples"]
    assert "Artist 0" in grade(sample)["unverified_examples_not_graded"]


def test_invented_title_duplicate_and_outside_rage_detected(sample):
    sample.result.cards[0].title = "Invented"
    assert not grade(sample)["checks"]["grounded"]
    sample.result.cards[1] = sample.result.cards[0]
    assert not grade(sample)["checks"]["no_duplicate_releases"]
    sample.catalog.releases[0].genre_ids = ["hip-hop"]
    assert not grade(sample)["checks"]["required_release_genre"]
    sample.filters.mode = "explore"
    assert not grade(sample)["checks"]["requested_mode"]


def test_name_matching_does_not_use_substrings(sample):
    sample.catalog.artists[0].name = "Cheese"
    sample.case["positive_artists"] = ["Che"]
    row = catalog_coverage(sample.dataset, sample.catalog)["cases"][0]["examples"]["positive"][0]
    assert row["catalog_name_match_ids"] == []


def test_budget_shares_daily_limit_and_keeps_unknown_cost(tmp_path):
    ledger = EvaluationLedger(tmp_path / "shared.sqlite3")
    first = ledger.reserve(.004, .1)
    ledger.settle(first, .001)
    ledger.settle(first, .001)  # repeated settlement must not reduce run spend
    ledger.reserve(.006, .1)
    assert ledger.run_spend == pytest.approx(.007)
    with pytest.raises(LLMError, match="0.01"):
        ledger.reserve(.004, .1)
    assert BudgetLedger(ledger.path).total() == pytest.approx(.007)
    BudgetLedger(ledger.path).reserve(.092, .10)
    with pytest.raises(LLMError) as error:
        ledger.reserve(.002, .10)
    assert error.value.code == "DAILY_BUDGET"
    assert ledger.run_spend == pytest.approx(.007)


@pytest.mark.parametrize("response_kind", ["proposal", "clarify", "error"])
def test_runner_uses_real_chat_without_gold_leak_or_replay_call(sample, tmp_path, response_kind):
    calls = []
    settings = OpenRouterSettings(api_key="test", model="test", input_usd_per_million=.1,
        output_usd_per_million=.3, request_cost_limit=.01, daily_cost_limit=.1)
    ledger = EvaluationLedger(tmp_path / "ledger.sqlite3")
    action = {"action": "recommend", "requested_artist_ids": [], "reference_artist_ids": [], "excluded_artist_ids": [],
              "preferred_descriptors": [], "avoided_descriptors": [], "genre_scope_id": "hip-hop", "mode": "surprise",
              "obscurity": "prefer", "locked_subgenre_ids": ["rage"],
              "exclude_known_artists": False, "count": 5, "release_id": None,
              "unsupported_preferences": [], "clarification": "none"}
    if response_kind == "clarify":
        action.update(action="clarify", locked_subgenre_ids=[], clarification="ask_preferences")

    def respond(request):
        calls.append(json.loads(request.content))
        if response_kind == "error":
            return httpx.Response(401, json={"error": "test"})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(action)}}],
                                        "usage": {"cost": .0001}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        result = run_case(sample.dataset, sample.case, sample.catalog,
                          lambda: OpenRouterClient(settings, ledger, http))
    assert len(calls) == 1
    prompt = json.dumps(calls, ensure_ascii=False)
    for gold in ["Secret positive", "Secret negative", "SECRET GOLD RATIONALE"]:
        assert gold not in prompt
    assert "Слушаю Artist 0" in prompt
    assert result["grading"]["checks"]["idempotency"]
    assert result["grading"]["checks"]["no_feedback_created"]
    if response_kind == "proposal":
        assert result["test_confirmed_proposal"]
        assert result["grading"]["checks"]["grounded"]
        assert result["grading"]["checks"]["requested_mode"]
        # Soft novelty quotas can use the remaining eligible pool without widening genre.
        assert result["grading"]["checks"]["five_results"]
        assert len(result["messages"]) == 2
    else:
        assert not result["test_confirmed_proposal"]
        assert not result["grading"]["structural_passed"]
        assert not result["messages"][-1]["cards"]


def test_committed_dataset_sources_and_excluded_slug():
    dataset = json.loads((PROJECT_ROOT / "data/evals/user-recommendation-cases-v1.json").read_text())
    eligible = eligible_names(dataset)
    from discovery.evals.user_recommendations import name_key
    for case in dataset["cases"]:
        assert all(name_key(n) in eligible for n in case["positive_artists"] + case["negative_artists"])
    assert "Slug" not in dataset["cases"][1]["positive_artists"]
    assert "Slug" in dataset["cases"][1]["positive_artists_text"]


def test_coverage_report_does_not_claim_chat_was_tested(sample):
    report = {"timestamp": "test", "catalog_snapshot": "fixture", "live_api_verified": False,
              "coverage": catalog_coverage(sample.dataset, sample.catalog), "cases": []}
    assert "Ответы бота не проверены" in markdown_report(report)
