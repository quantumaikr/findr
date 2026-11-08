"""while the photo server is still loading (search no longer waits for it, #57), questions do not wait for it either. On
Windows a refused localhost connection takes ~2 s, and each question used to spend PHOTO_WAIT on it"""
import socket
import time
import urllib.request

import pytest

import search


def _closed_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def test_loading_server_costs_nothing_and_is_looked_at_every_10s(monkeypatch):
    monkeypatch.setattr(search, "PHOTO_URL", f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setattr(search, "_PHOTO_UP", [0.0, False])
    t = time.time()
    with pytest.raises(ConnectionError):
        search.photo_text_vec("바다 사진")
    assert time.time() - t < 1.0
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("looked again within 10 s"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("asked a server that is not up"))
    with pytest.raises(ConnectionError):
        search.photo_text_vec("바다 사진")


def test_answering_server_is_asked(monkeypatch):
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    monkeypatch.setattr(search, "PHOTO_URL", f"http://127.0.0.1:{srv.getsockname()[1]}")
    monkeypatch.setattr(search, "_PHOTO_UP", [0.0, False])
    try:
        assert search._photo_up() is True
    finally:
        srv.close()
