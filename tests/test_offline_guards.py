import os
import socket

import pytest


def test_outbound_network_is_blocked():
    with pytest.raises(RuntimeError, match="Network access is disabled"):
        socket.create_connection(("203.0.113.10", 443), timeout=1)


def test_project_env_and_provider_key_are_disabled():
    assert os.environ["DISCOVERY_DOTENV"] == "0"
    assert "OPENROUTER_API_KEY" not in os.environ


def test_loopback_is_allowed():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        with socket.create_connection(server.getsockname(), timeout=1):
            pass
    finally:
        server.close()
