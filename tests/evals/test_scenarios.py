from discovery.evals.scenarios import run_scenarios


def test_release_invariants():
    results = run_scenarios()
    assert all(r["passed"] for r in results), results
