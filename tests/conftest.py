"""Test-wide guards: no project .env, no provider key, no outbound network.

The guards run at import time (before test modules import the app, which may
load the project .env) and again per test, so a test that forgets to set up a
mock transport fails loudly instead of making a real, possibly paid, call.
"""

import ipaddress
import os
import socket

import pytest

os.environ["DISCOVERY_DOTENV"] = "0"
os.environ.pop("OPENROUTER_API_KEY", None)

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _is_local(address) -> bool:
    if not isinstance(address, tuple):
        return True  # AF_UNIX paths and other local transports
    host = str(address[0])
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def _guarded(real):
    def connect(self, address):
        if not _is_local(address):
            raise RuntimeError(f"Network access is disabled in tests: {address!r}")
        return real(self, address)
    return connect


@pytest.fixture(autouse=True)
def _offline_guards(monkeypatch):
    monkeypatch.setenv("DISCOVERY_DOTENV", "0")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(socket.socket, "connect", _guarded(_real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded(_real_connect_ex))
