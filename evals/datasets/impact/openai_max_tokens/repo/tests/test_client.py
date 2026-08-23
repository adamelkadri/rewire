"""Tests call the API too, and a migration that skips them leaves a red build."""

from app.client import ask


def test_ask():
    assert ask("hi", max_tokens=10)
