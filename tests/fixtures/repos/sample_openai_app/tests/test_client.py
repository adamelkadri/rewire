"""Tests exercise the same API, and must be distinguishable from source."""

from chatapp.client import ChatClient


def test_generate(monkeypatch):
    client = ChatClient()
    assert client.generate("hi", max_tokens=10)
