"""Tests for reading a watched specification, against a stubbed transport.

``httpx.MockTransport`` rather than a live server: what is being tested is the
policy Rewire applies to a response it did not ask a person about — the size
ceiling, the scheme rules, the conditional request, and the headers it does
*not* send. None of that needs a socket, and a test that opened one would be
testing the network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from rewire.core.errors import WatchError
from rewire.watch import source
from rewire.watch.source import Fetched, digest_of, fetch, fetch_file, fetch_url, is_url

SPEC = "openapi: 3.0.3\ninfo: {title: T, version: '1'}\npaths: {}\n"


class Transport:
    """Records the requests it was handed and answers them from a script."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a stub transport and hand back a factory for arming it."""

    def arm(*responses: httpx.Response) -> Transport:
        handler = Transport(*responses)
        real_client = httpx.Client

        def factory(**kwargs: Any) -> httpx.Client:
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(source.httpx, "Client", factory)
        return handler

    return arm


# -------------------------------------------------------------------- URLs ---


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("https://example.test/o.yaml", True),
        ("http://example.test/o.yaml", True),
        ("./specs/o.yaml", False),
        ("/tmp/o.yaml", False),  # noqa: S108 - a path literal, nothing is written
        ("file:///tmp/o.yaml", False),
    ],
)
def test_a_source_is_a_url_only_when_it_names_a_network(candidate: str, expected: bool) -> None:
    assert is_url(candidate) is expected


def test_plain_http_is_refused_by_default() -> None:
    with pytest.raises(WatchError, match="plain HTTP"):
        fetch_url("http://example.test/o.yaml")


def test_plain_http_is_permitted_when_asked_for(transport: Any) -> None:
    transport(httpx.Response(200, text=SPEC))
    assert fetch_url("http://example.test/o.yaml", allow_http=True).text == SPEC


def test_an_unsupported_scheme_is_refused() -> None:
    """A monitor that would read ``file://`` from a URL field is a file-disclosure bug."""
    with pytest.raises(WatchError, match="unsupported URL scheme"):
        fetch_url("file:///etc/passwd")


def test_an_https_url_that_redirects_to_http_is_refused(transport: Any) -> None:
    """The document still arrived over plain HTTP, whatever the first URL said."""
    transport(
        httpx.Response(302, headers={"location": "http://example.test/o.yaml"}),
        httpx.Response(200, text=SPEC),
    )
    with pytest.raises(WatchError, match="plain HTTP"):
        fetch_url("https://example.test/o.yaml")


# ------------------------------------------------------------------ bodies ---


def test_a_successful_fetch_carries_the_document_and_its_validators(transport: Any) -> None:
    transport(
        httpx.Response(
            200,
            text=SPEC,
            headers={"etag": '"abc"', "last-modified": "Mon, 01 Jan 2024 00:00:00 GMT"},
        )
    )
    fetched = fetch_url("https://example.test/o.yaml")
    assert fetched.text == SPEC
    assert fetched.etag == '"abc"'
    assert fetched.last_modified.startswith("Mon")
    assert fetched.not_modified is False
    assert fetched.digest == digest_of(SPEC)


def test_validators_are_sent_back_on_the_next_fetch(transport: Any) -> None:
    """The whole point of storing them: the ordinary answer becomes 304."""
    handler = transport(httpx.Response(304))
    fetched = fetch_url(
        "https://example.test/o.yaml", etag='"abc"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT"
    )
    request = handler.requests[0]
    assert request.headers["if-none-match"] == '"abc"'
    assert request.headers["if-modified-since"].startswith("Mon")
    assert fetched.not_modified is True
    assert fetched.text == ""


def test_a_304_keeps_the_validators_it_was_given(transport: Any) -> None:
    """A 304 need not repeat the ETag, and losing it would end the conditioning."""
    transport(httpx.Response(304))
    fetched = fetch_url("https://example.test/o.yaml", etag='"abc"')
    assert fetched.etag == '"abc"'


def test_nothing_authenticating_is_ever_sent(transport: Any) -> None:
    """Structural: there is no parameter for it, so there is nothing to leak."""
    handler = transport(httpx.Response(200, text=SPEC))
    fetch_url("https://example.test/o.yaml")
    sent = {key.lower() for key in handler.requests[0].headers}
    assert not sent & {"authorization", "cookie", "proxy-authorization"}
    assert handler.requests[0].headers["user-agent"] == source.USER_AGENT


def test_an_error_status_is_a_readable_error(transport: Any) -> None:
    transport(httpx.Response(404))
    with pytest.raises(WatchError, match="HTTP 404"):
        fetch_url("https://example.test/o.yaml")


def test_a_transport_failure_becomes_a_domain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    real_client = httpx.Client
    monkeypatch.setattr(
        source.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(boom), **kwargs),
    )
    with pytest.raises(WatchError, match="could not be fetched"):
        fetch_url("https://example.test/o.yaml")


def test_a_declared_length_over_the_ceiling_is_refused_before_reading(transport: Any) -> None:
    transport(httpx.Response(200, text=SPEC, headers={"content-length": "999999999"}))
    with pytest.raises(WatchError, match="larger than the safety limit"):
        fetch_url("https://example.test/o.yaml", max_bytes=1024)


def test_a_body_that_grows_past_the_ceiling_is_refused_while_reading(transport: Any) -> None:
    """The declared length is a claim. This is the measurement."""
    body = b"x" * 5000
    transport(httpx.Response(200, content=body, headers={"content-length": "10"}))
    with pytest.raises(WatchError, match="larger than the safety limit"):
        fetch_url("https://example.test/o.yaml", max_bytes=1024)


def test_a_body_that_is_not_utf8_is_a_readable_error(transport: Any) -> None:
    transport(httpx.Response(200, content=b"\xff\xfe not text"))
    with pytest.raises(WatchError, match="not valid UTF-8"):
        fetch_url("https://example.test/o.yaml")


@pytest.mark.parametrize(
    ("url", "suffix"),
    [
        ("https://e.test/o.json", ".json"),
        ("https://e.test/o.yml", ".yml"),
        ("https://e.test/o.yaml", ".yaml"),
        ("https://e.test/openapi", ".yaml"),
        ("https://e.test/o.json?v=2", ".json"),
    ],
)
def test_the_stored_copy_is_named_after_the_source(transport: Any, url: str, suffix: str) -> None:
    transport(httpx.Response(200, text=SPEC))
    assert fetch_url(url).suffix == suffix


# ------------------------------------------------------------------- files ---


def test_a_local_specification_is_read(tmp_path: Path) -> None:
    path = tmp_path / "openapi.yaml"
    path.write_text(SPEC, encoding="utf-8")
    fetched = fetch_file(path)
    assert fetched.text == SPEC
    assert fetched.source == str(path)


def test_a_missing_local_specification_is_a_readable_error(tmp_path: Path) -> None:
    with pytest.raises(WatchError, match="could not be read"):
        fetch_file(tmp_path / "absent.yaml")


def test_an_oversized_local_specification_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "openapi.yaml"
    path.write_text("x" * 4096, encoding="utf-8")
    with pytest.raises(WatchError, match="larger than the safety limit"):
        fetch_file(path, max_bytes=100)


def test_a_local_specification_that_is_not_utf8_is_a_readable_error(tmp_path: Path) -> None:
    path = tmp_path / "openapi.yaml"
    path.write_bytes(b"\xff\xfe not text")
    with pytest.raises(WatchError, match="could not be read"):
        fetch_file(path)


def test_fetch_dispatches_on_the_shape_of_the_source(tmp_path: Path, transport: Any) -> None:
    path = tmp_path / "openapi.yaml"
    path.write_text(SPEC, encoding="utf-8")
    assert fetch(str(path)).text == SPEC

    transport(httpx.Response(200, text=SPEC))
    assert fetch("https://example.test/o.yaml").text == SPEC


def test_a_digest_is_over_the_document_and_nothing_else() -> None:
    assert Fetched(text=SPEC).digest == digest_of(SPEC)
    assert digest_of(SPEC) != digest_of(SPEC + "\n")
