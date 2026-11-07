"""Server-selected account stores; clients never choose a database path."""

from fastapi import Request

from discovery.storage import Store

from .auth import authenticate
from .schemas import User

_PROTECTED: set = set()


def get_user(request: Request) -> User:
    return authenticate(request).user


def get_store(request: Request) -> Store:
    user = get_user(request)
    path = request.app.state.data_dir / "users" / user.id / "discovery.sqlite3"
    store = Store(path)
    if path not in _PROTECTED:  # once per process, not per request
        path.chmod(0o600)
        _PROTECTED.add(path)
    return store
