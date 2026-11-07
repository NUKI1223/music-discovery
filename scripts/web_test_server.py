"""Browser test server: synthetic catalog, isolated data, no paid API calls."""

import argparse
import json
from pathlib import Path

import httpx
import uvicorn

from discovery.catalog.demo import make_demo_catalog
from discovery.llm import BudgetLedger, OpenRouterClient, OpenRouterSettings
from discovery.web.app import create_app
from discovery.web.schemas import Artwork


def mock_client_factory(directory: Path):
    """Exercise the real structured-output boundary without leaving this process."""
    def respond(request):
        payload = json.loads(request.content)
        messages = payload["messages"]
        prompt = messages[-1]["content"]
        if "network-error" in prompt:
            return httpx.Response(503)
        context = json.loads('{"filters":' + messages[0]["content"].split('{"filters":', 1)[1])
        filters = context["filters"]
        action = {"action": "recommend", **filters, "count": 5, "release_id": None,
                  "unsupported_preferences": [], "clarification": "none"}
        if "неожидан" in prompt.casefold():
            action["mode"] = "surprise"
        if "которых я знаю" in prompt.casefold():
            action["exclude_known_artists"] = True
        if "жесткости" in prompt.casefold():
            action["locked_subgenre_ids"] = ["hardcore"]
        if "исполнителя-ориентира" in prompt and context["mentioned_artists"]:
            artist_id = context["mentioned_artists"][0]["id"]
            action.update(reference_artist_ids=[artist_id], excluded_artist_ids=[artist_id])
        if context["selected_release_id"]:
            action.update(action="explain", release_id=context["selected_release_id"])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(action)}}],
                                         "usage": {"cost": 0}})

    def factory():
        client = OpenRouterClient(
            OpenRouterSettings(api_key="mock-only", model="mock-model",
                               input_usd_per_million=.075, output_usd_per_million=.20,
                               request_cost_limit=.01, daily_cost_limit=.10),
            BudgetLedger(directory / "mock-budget.sqlite3"),
            http_client=httpx.Client(transport=httpx.MockTransport(respond)), sleep=lambda _: None)
        client._owns_http = True
        return client

    return factory


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    app = create_app(data_dir=args.data_dir, catalog=make_demo_catalog(),
                     client_factory=mock_client_factory(args.data_dir))
    app.state.artwork_cache.resolve = lambda title, artists, aoty_url=None, year=None: Artwork()
    # The browser suite creates many isolated accounts from one loopback IP.
    # Keep real login/password throttling; production registration limits are
    # covered by API security tests and are unchanged by this test-only override.
    throttle = app.state.accounts.throttle
    app.state.accounts.throttle = lambda buckets: throttle([
        (key, 200 if key.startswith("register:") else limit) for key, limit in buckets
    ])
    uvicorn.run(app, host="127.0.0.1", port=args.port)
