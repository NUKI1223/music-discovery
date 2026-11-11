"""Real HTTP responses must satisfy the contract used to generate browser types."""

import importlib.util
import json
from pathlib import Path

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from discovery.catalog.demo import make_demo_catalog
from discovery.web.app import create_app

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "docs/web-openapi.json").read_text())


def validate(response, path, method, status=200):
    assert response.status_code == status, response.text
    schema = CONTRACT["paths"][path][method]["responses"][str(status)]["content"][
        "application/json"]["schema"]
    Draft202012Validator({**schema, "components": CONTRACT["components"]}).validate(response.json())
    return response.json()


def test_contract_export_is_current():
    spec = importlib.util.spec_from_file_location("export_contract", ROOT / "scripts/export_web_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.contract() == CONTRACT


def test_browser_contract_real_responses(tmp_path):
    app = create_app(data_dir=tmp_path, catalog=make_demo_catalog(),
                     allowed_origins={"http://testserver"})
    with TestClient(app) as client:
        assert client.get("/openapi.json").json() == CONTRACT
        client.headers["Origin"] = "http://testserver"
        validate(client.get("/api/auth/session"), "/api/auth/session", "get", 401)
        validate(client.post("/api/auth/register", json={"username": "bad"}),
                 "/api/auth/register", "post", 422)
        creds = {"username": "contract", "password": "contract-password-123"}
        session = validate(client.post("/api/auth/register", json={
            **creds, "password_confirmation": creds["password"]}), "/api/auth/register", "post")
        client.headers["X-CSRF-Token"] = session["csrf_token"]
        validate(client.get("/api/auth/session"), "/api/auth/session", "get")
        profile = validate(client.get("/api/profile"), "/api/profile", "get")
        assert profile["seed_artist_ids"] == []
        profile["onboarded"] = True
        validate(client.put("/api/profile", json=profile), "/api/profile", "put")
        validate(client.post("/api/profile/starter"), "/api/profile/starter", "post")
        validate(client.get("/api/catalog/genres?q=hip"), "/api/catalog/genres", "get")
        scoped = validate(client.get("/api/catalog/genres?genre_scope_id=hip-hop"),
                          "/api/catalog/genres", "get")
        assert "modal-jazz" not in {item["id"] for item in scoped}
        assert "experimental" in {item["id"] for item in scoped}
        validate(client.get("/api/catalog/artists?q=artist"), "/api/catalog/artists", "get")
        selected = validate(client.get("/api/catalog/artists?ids=demo-artist-trap-47"),
                            "/api/catalog/artists", "get")
        assert [a["id"] for a in selected] == ["demo-artist-trap-47"]
        assert validate(client.get("/api/collection"), "/api/collection", "get") == []
        chat = validate(client.post("/api/chats", json={"filters": profile["filters"]}),
                        "/api/chats", "post")
        url = f"/api/chats/{chat['id']}"
        validate(client.get("/api/chats"), "/api/chats", "get")
        validate(client.get(url), "/api/chats/{chat_id}", "get")
        validate(client.patch(url + "/filters", json=profile["filters"]),
                 "/api/chats/{chat_id}/filters", "patch")
        message = validate(client.post(url + "/messages", json={
            "message_id": "contract-message-001", "text": "Подбери музыку", "apply_filters": True}),
            "/api/chats/{chat_id}/messages", "post")
        assert message["cards"]
        card = message["cards"][0]
        validate(client.post("/api/feedback", json={"feedback_id": "contract-feedback-001",
            "decision_id": card["decision_id"], "release_id": card["release_id"],
            "kind": "listened_like"}), "/api/feedback", "post")
        collection = validate(client.get("/api/collection"), "/api/collection", "get")
        assert collection[0]["feedback"] == "listened_like"
        validate(client.delete(f"/api/collection/{card['release_id']}?forget=true"),
                 "/api/collection/{release_id}", "delete")
        validate(client.delete("/api/collection"), "/api/collection", "delete")
        validate(client.delete(url), "/api/chats/{chat_id}", "delete")
        validate(client.get(url), "/api/chats/{chat_id}", "get", 404)
        validate(client.get("/api/chats/nonexistent"), "/api/chats/{chat_id}", "get", 404)
        validate(client.post("/api/auth/logout"), "/api/auth/logout", "post")
        session = validate(client.post("/api/auth/login", json=creds), "/api/auth/login", "post")
        client.headers["X-CSRF-Token"] = session["csrf_token"]
        validate(client.post("/api/auth/password", json={"current_password": creds["password"],
            "new_password": "contract-new-password-123"}), "/api/auth/password", "post")


def test_app_page_is_revalidated_by_browsers(tmp_path):
    import pytest

    app = create_app(data_dir=tmp_path, catalog=make_demo_catalog(),
                     allowed_origins={"http://testserver"})
    with TestClient(app) as client:
        for path in ("/", "/collection"):
            response = client.get(path)
            if response.status_code == 503:
                pytest.skip("frontend/dist/index.html is not built")
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-cache"
