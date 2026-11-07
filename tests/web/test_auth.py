from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from discovery.catalog.demo import make_demo_catalog
from discovery.web.app import create_app
from discovery.web.auth import COOKIE, client_ip, token_hash

PASSWORD = "correct-horse-123"


@pytest.fixture
def app(tmp_path):
    return create_app(data_dir=tmp_path / "web", catalog=make_demo_catalog(),
                      allowed_origins=["http://testserver"])


@pytest.fixture
def client(app):
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        yield client


def register(client, username="alice"):
    result = client.post("/api/auth/register", json={"username": username, "password": PASSWORD,
                          "password_confirmation": PASSWORD})
    assert result.status_code == 200, result.text
    client.headers["X-CSRF-Token"] = result.json()["csrf_token"]
    return result


def test_register_hash_cookie_login_logout(client, app):
    result = register(client, "ALIce")
    assert result.json()["user"]["username"] == "alice"
    assert "HttpOnly" in result.headers["set-cookie"]
    assert "SameSite=lax" in result.headers["set-cookie"]
    assert "Max-Age=604800" in result.headers["set-cookie"]
    row = app.state.accounts.by_name("Alice")
    assert row["password_hash"].startswith("$argon2")
    assert PASSWORD not in row["password_hash"]
    token = client.cookies.get(COOKIE)
    with app.state.accounts.connection() as conn:
        assert conn.execute("SELECT token_hash FROM sessions").fetchone()[0] == token_hash(token)
    assert client.get("/api/auth/session").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/session").status_code == 401
    client.cookies.set(COOKIE, token)
    assert client.get("/api/auth/session").status_code == 401
    result = client.post("/api/auth/login", json={"username": "aLiCe", "password": PASSWORD})
    assert result.status_code == 200
    assert client.post("/api/auth/register", json={"username": "alice", "password": PASSWORD,
                       "password_confirmation": PASSWORD}).status_code == 409


def test_invalid_login_validation_and_expiry(client, app):
    register(client)
    assert client.post("/api/auth/login", json={"username": "alice", "password": "wrong-password"}).status_code == 401
    bad = client.post("/api/auth/register", json={"username": "bad user", "password": "SECRET",
                      "password_confirmation": "SECRET"})
    assert bad.status_code == 422
    assert isinstance(bad.json()["detail"], str)
    assert "SECRET" not in bad.text
    with app.state.accounts.connection() as conn:
        conn.execute("UPDATE sessions SET expires_at=0")
    assert client.get("/api/auth/session").status_code == 401


def test_origin_csrf_and_dns_rebinding(client):
    assert client.post("/api/auth/login", headers={"Origin": "https://evil.example"},
                       json={"username": "alice", "password": PASSWORD}).status_code == 403
    register(client)
    for path in ("/api/auth/logout", "/api/profile/starter"):
        assert client.post(path, headers={"X-CSRF-Token": "wrong"}).status_code == 403
        assert client.post(path, headers={"Origin": "http://testserver:9000"}).status_code == 403
    assert client.get("/api/profile", headers={"Host": "evil.example"}).status_code == 400
    client.headers.pop("Origin")
    assert client.post("/api/auth/logout").status_code == 403


def test_password_change_revokes_all_sessions(client, app):
    register(client)
    other = TestClient(app, headers={"Origin": "http://testserver"})
    assert other.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200
    assert client.post("/api/auth/password", json={"current_password": "wrong", "new_password": "new-long-password"}).status_code == 400
    assert client.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "new-long-password"}).status_code == 200
    assert other.get("/api/auth/session").status_code == 401
    assert client.get("/api/auth/session").status_code == 401
    assert client.post("/api/auth/login", json={"username": "alice", "password": "new-long-password"}).status_code == 200


def test_login_limit_survives_app_restart(client, app):
    register(client)
    for _ in range(10):
        assert client.post("/api/auth/login", json={"username": "alice", "password": "wrong-password"}).status_code == 401
    restarted = create_app(data_dir=app.state.data_dir, catalog=app.state.catalog,
                           allowed_origins=["http://testserver"])
    with TestClient(restarted, headers={"Origin": "http://testserver"}) as again:
        assert again.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 429


def test_registration_unique_under_concurrency(app):
    def create():
        with TestClient(app, headers={"Origin": "http://testserver"}) as client:
            return client.post("/api/auth/register", json={"username": "same", "password": PASSWORD,
                               "password_confirmation": PASSWORD}).status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: create(), range(2))) == [200, 409]


def test_request_body_limit_before_json_validation(client):
    result = client.post("/api/auth/register", content=b"x" * 32769,
                         headers={"Content-Type": "application/json"})
    assert result.status_code == 413


def login(client, password=PASSWORD, username="alice"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_successful_logins_are_not_throttled(client):
    register(client)
    for _ in range(11):
        assert login(client).status_code == 200


def test_failures_from_other_ip_do_not_block_owner(app):
    with TestClient(app, headers={"Origin": "http://testserver"}, client=("10.0.0.1", 1)) as owner:
        register(owner)
    with TestClient(app, headers={"Origin": "http://testserver"}, client=("10.0.0.2", 1)) as attacker:
        for _ in range(10):
            assert login(attacker, "wrong-password").status_code == 401
        assert login(attacker, "wrong-password").status_code == 429
    with TestClient(app, headers={"Origin": "http://testserver"}, client=("10.0.0.1", 1)) as owner:
        assert login(owner).status_code == 200


def test_success_resets_failure_counter(client):
    register(client)
    for _ in range(9):
        assert login(client, "wrong-password").status_code == 401
    assert login(client).status_code == 200
    for _ in range(9):
        assert login(client, "wrong-password").status_code == 401
    assert login(client).status_code == 200


def test_unknown_user_same_response_and_counted(client):
    first = login(client, username="ghost")
    assert first.status_code == 401
    register(client)
    assert login(client, "wrong-password").json() == first.json()


def test_missing_client_address_does_not_crash(app):
    assert client_ip(Request({"type": "http", "client": None, "headers": []})) == "unknown"


def test_parallel_wrong_passwords_cannot_exceed_the_limit(app):
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        register(client)

        def attempt(_):
            return login(client, "wrong-password").status_code
        with ThreadPoolExecutor(max_workers=16) as pool:
            codes = list(pool.map(attempt, range(30)))
    assert codes.count(401) <= 10
    assert codes.count(429) >= 20
