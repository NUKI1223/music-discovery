"""Local accounts, revocable opaque sessions and persistent login throttling."""

import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from pwdlib import PasswordHash

from .schemas import Credentials, Ok, PasswordChange, Registration, Session, User

COOKIE = "discovery_session"
SESSION_SECONDS = 7 * 24 * 60 * 60
password_hasher = PasswordHash.recommended()
_dummy_hash = password_hasher.hash(secrets.token_urlsafe(32))
router = APIRouter(prefix="/api/auth", tags=["auth"])


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    csrf_token TEXT NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_attempts (
                    bucket TEXT NOT NULL, attempted_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS auth_attempt_bucket ON auth_attempts(bucket,attempted_at);
                CREATE TABLE IF NOT EXISTS legacy_imports (
                    user_id TEXT PRIMARY KEY REFERENCES users(id), source TEXT NOT NULL,
                    imported_at REAL NOT NULL
                );
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def throttle(self, buckets: list[tuple[str, int]]) -> None:
        now = time.time()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM auth_attempts WHERE attempted_at<?", (now - 900,))
            for key, limit in buckets:
                count = conn.execute("SELECT count(*) FROM auth_attempts WHERE bucket=?", (key,)).fetchone()[0]
                if count >= limit:
                    raise HTTPException(429, "Слишком много попыток. Повторите через 15 минут.")
            conn.executemany("INSERT INTO auth_attempts VALUES (?,?)", [(key, now) for key, _ in buckets])

    def reserve_attempt(self, buckets: list[tuple[str, int]]) -> float:
        """Atomically check the limits and count this attempt before the password check.

        Concurrent guesses therefore cannot all pass a not-yet-updated counter. A
        successful login gives the slot back (release_attempt), so only failures remain.
        """
        now = time.time()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM auth_attempts WHERE attempted_at<?", (now - 900,))
            for key, limit in buckets:
                count = conn.execute("SELECT count(*) FROM auth_attempts WHERE bucket=?", (key,)).fetchone()[0]
                if count >= limit:
                    raise HTTPException(429, "Слишком много попыток. Повторите через 15 минут.")
            conn.executemany("INSERT INTO auth_attempts VALUES (?,?)", [(key, now) for key, _ in buckets])
        return now

    def release_attempt(self, keys: list[str], reserved_at: float, clear: str | None = None) -> None:
        """Undo one reserved attempt; optionally forget all failures of a bucket."""
        with self.connection() as conn:
            for key in keys:
                conn.execute("DELETE FROM auth_attempts WHERE rowid IN (SELECT rowid FROM auth_attempts "
                             "WHERE bucket=? AND attempted_at=? LIMIT 1)", (key, reserved_at))
            if clear is not None:
                conn.execute("DELETE FROM auth_attempts WHERE bucket=?", (clear,))

    def by_name(self, username: str):
        with self.connection() as conn:
            return conn.execute("SELECT * FROM users WHERE username=?", (username.lower(),)).fetchone()

    def register(self, data: Registration) -> User:
        if data.password != data.password_confirmation:
            raise HTTPException(400, "Пароли не совпадают.")
        user = User(id=uuid4().hex, username=data.username.lower())
        hashed = password_hasher.hash(data.password)
        try:
            with self.connection() as conn:
                conn.execute("INSERT INTO users VALUES (?,?,?,?)", (user.id, user.username, hashed, time.time()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Этот логин уже занят.") from None
        return user

    def login(self, data: Credentials) -> User:
        row = self.by_name(data.username)
        valid = password_hasher.verify(data.password, row["password_hash"] if row else _dummy_hash)
        if not valid or row is None:
            raise HTTPException(401, "Неверный логин или пароль.")
        return User(id=row["id"], username=row["username"])

    def new_session(self, user: User, response: Response, secure: bool) -> Session:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connection() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
            conn.execute("INSERT INTO sessions VALUES (?,?,?,?)", (token_hash(token), user.id, csrf, time.time() + SESSION_SECONDS))
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                            secure=secure, samesite="lax", path="/")
        return Session(user=user, csrf_token=csrf)

    def session(self, token: str) -> Session:
        with self.connection() as conn:
            row = conn.execute("SELECT u.id,u.username,s.csrf_token FROM sessions s JOIN users u "
                               "ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?",
                               (token_hash(token), time.time())).fetchone()
        if row is None:
            raise HTTPException(401, "Войдите в аккаунт.")
        return Session(user=User(id=row["id"], username=row["username"]), csrf_token=row["csrf_token"])

    def reset_password(self, user_id: str, password: str) -> None:
        if not 12 <= len(password) <= 128:
            raise ValueError("Пароль должен содержать от 12 до 128 символов.")
        hashed = password_hasher.hash(password)
        with self.connection() as conn:
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hashed, user_id))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def authenticate(request: Request) -> Session:
    session = request.app.state.accounts.session(request.cookies.get(COOKIE, ""))
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        supplied = request.headers.get("X-CSRF-Token", "")
        if not secrets.compare_digest(supplied.encode(), session.csrf_token.encode()):
            raise HTTPException(403, "Недействительный CSRF-токен. Обновите страницу.")
    return session


@router.post("/register", response_model=Session)
def register(data: Registration, request: Request, response: Response):
    accounts = request.app.state.accounts
    accounts.throttle([("register:" + client_ip(request), 20)])
    user = accounts.register(data)
    return accounts.new_session(user, response, request.url.scheme == "https")


@router.post("/login", response_model=Session)
def login(data: Credentials, request: Request, response: Response):
    accounts = request.app.state.accounts
    ip = client_ip(request)
    pair, ip_key = f"login:{data.username.lower()}|{ip}", "ip:" + ip
    # Only failed attempts remain counted, and a login bucket is per (username, ip).
    # The slot is reserved before the slow password check so parallel guesses are bounded.
    reserved_at = accounts.reserve_attempt([(pair, 10), (ip_key, 100)])
    try:
        user = accounts.login(data)
    except HTTPException as error:
        if error.status_code != 401:
            accounts.release_attempt([pair, ip_key], reserved_at)
        raise
    except BaseException:
        accounts.release_attempt([pair, ip_key], reserved_at)
        raise
    accounts.release_attempt([ip_key], reserved_at, clear=pair)
    return accounts.new_session(user, response, request.url.scheme == "https")


@router.get("/session", response_model=Session)
def session(request: Request):
    return authenticate(request)


@router.post("/logout", response_model=Ok)
def logout(request: Request, response: Response):
    authenticate(request)
    with request.app.state.accounts.connection() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(request.cookies.get(COOKIE, "")),))
    response.delete_cookie(COOKIE, path="/")
    return Ok()


@router.post("/password", response_model=Ok)
def password(data: PasswordChange, request: Request, response: Response):
    current = authenticate(request)
    accounts = request.app.state.accounts
    accounts.throttle([("password:" + current.user.id, 10)])
    row = accounts.by_name(current.user.username)
    if not password_hasher.verify(data.current_password, row["password_hash"]):
        raise HTTPException(400, "Текущий пароль неверен.")
    accounts.reset_password(current.user.id, data.new_password)
    response.delete_cookie(COOKIE, path="/")
    return Ok()
