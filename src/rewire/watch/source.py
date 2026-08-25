"""Reading a watched specification, from a URL or from disk.

This is the second place in Rewire that reaches outside the machine, and unlike
publishing it does so on a timer with nobody watching. That shapes every choice
here.

**The response is untrusted and is bounded before it is believed.** It is read in
chunks against a byte ceiling rather than downloaded and then measured, because
a server that answers a monitor every hour is in a good position to hand it a
decompression bomb. The declared ``Content-Length`` is checked too, but only as
a cheap first refusal — it is a claim, not a measurement.

**Nothing authenticates.** No header, no token, no netrc, no cookie jar. A
specification behind a credential is out of scope, which costs a real capability
and buys the guarantee that a monitored URL cannot exfiltrate anything, because
there is nothing to send.

**Plain HTTP is refused by default,** including after a redirect. A monitor
trusting an unauthenticated document over an unauthenticated channel is trusting
whoever is between it and the server, and this one goes on to call a model and
open a pull request.

**Cache validators are used.** ``ETag`` and ``Last-Modified`` are handed back on
the next check, so the ordinary answer is 304 and no body at all. On an hourly
schedule that is the difference between a monitor a public API tolerates and one
it rate-limits.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

import httpx

from rewire.core.errors import WatchError
from rewire.core.logging import get_logger

logger = get_logger(__name__)

#: Identifies Rewire to the servers it polls, so an operator seeing this in a
#: log can find out what it is and how to make it stop.
USER_AGENT: Final[str] = "rewire-watch/0.1 (+https://github.com/adamelkadri/rewire)"

#: Bytes read per chunk while enforcing the ceiling.
CHUNK_BYTES: Final[int] = 64 * 1024

#: Redirects followed before giving up.
MAX_REDIRECTS: Final[int] = 5

#: Schemes that may be fetched over the network.
_NETWORK_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})


@dataclass(frozen=True, slots=True)
class Fetched:
    """One reading of a watched specification."""

    #: The document. Empty when ``not_modified`` is set.
    text: str = ""
    #: The server said the cached copy is still current, so nothing was read.
    not_modified: bool = False
    etag: str = ""
    last_modified: str = ""
    #: Where it came from, for error messages and metadata.
    source: str = ""
    #: Extension to give the stored copy. Cosmetic: the parser sniffs content.
    suffix: str = ".yaml"

    @property
    def digest(self) -> str:
        """SHA-256 of the document as read."""
        return digest_of(self.text)


def digest_of(text: str) -> str:
    """Return the SHA-256 hex digest of ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_url(source: str) -> bool:
    """Whether ``source`` names a network location rather than a file."""
    return urlparse(source).scheme in _NETWORK_SCHEMES


def _suffix_for(source: str) -> str:
    lowered = source.split("?", 1)[0].lower()
    if lowered.endswith(".json"):
        return ".json"
    if lowered.endswith(".yml"):
        return ".yml"
    return ".yaml"


def _require_allowed_scheme(url: str, *, allow_http: bool, context: str) -> None:
    scheme = urlparse(url).scheme
    if scheme == "https" or (scheme == "http" and allow_http):
        return
    if scheme == "http":
        raise WatchError(
            "refusing to fetch a specification over plain HTTP; "
            "pass --allow-http if the endpoint is genuinely trusted",
            url=context,
        )
    raise WatchError(f"unsupported URL scheme: {scheme or '(none)'}", url=context)


def _read_bounded(response: httpx.Response, *, max_bytes: int, url: str) -> bytes:
    """Read the body in chunks, refusing to exceed ``max_bytes``."""
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise WatchError(
            "the specification is larger than the safety limit",
            url=url,
            declared_bytes=int(declared),
            limit_bytes=max_bytes,
        )

    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes(CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            raise WatchError(
                "the specification is larger than the safety limit",
                url=url,
                limit_bytes=max_bytes,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_url(
    url: str,
    *,
    etag: str = "",
    last_modified: str = "",
    timeout_seconds: float = 30.0,
    max_bytes: int = 32 * 1024 * 1024,
    allow_http: bool = False,
) -> Fetched:
    """Fetch a specification over HTTP, conditionally when possible.

    Args:
        url: The location to read.
        etag: ``ETag`` from the previous fetch, sent as ``If-None-Match``.
        last_modified: ``Last-Modified`` from the previous fetch, sent as
            ``If-Modified-Since``.
        timeout_seconds: Ceiling on the whole request.
        max_bytes: Ceiling on the response body.
        allow_http: Permit plain HTTP, before and after redirects.

    Raises:
        WatchError: The scheme is not allowed, the request failed, the response
            was not a success, or the body exceeded ``max_bytes``.
    """
    _require_allowed_scheme(url, allow_http=allow_http, context=url)

    headers = {"user-agent": USER_AGENT, "accept": "application/json, application/yaml, */*"}
    if etag:
        headers["if-none-match"] = etag
    if last_modified:
        headers["if-modified-since"] = last_modified

    try:
        with (
            httpx.Client(
                timeout=timeout_seconds,
                follow_redirects=True,
                max_redirects=MAX_REDIRECTS,
                headers=headers,
            ) as client,
            client.stream("GET", url) as response,
        ):
            # Checked *after* the redirect chain: an https URL that redirects to
            # http has still delivered the document over plain HTTP.
            _require_allowed_scheme(str(response.url), allow_http=allow_http, context=url)

            if response.status_code == httpx.codes.NOT_MODIFIED:
                logger.debug("watch_not_modified", url=url)
                return Fetched(
                    not_modified=True,
                    etag=response.headers.get("etag", etag),
                    last_modified=response.headers.get("last-modified", last_modified),
                    source=url,
                    suffix=_suffix_for(url),
                )
            if response.status_code >= httpx.codes.BAD_REQUEST:
                raise WatchError(
                    f"the specification could not be fetched: HTTP {response.status_code}",
                    url=url,
                )
            body = _read_bounded(response, max_bytes=max_bytes, url=url)
            fetched_etag = response.headers.get("etag", "")
            fetched_modified = response.headers.get("last-modified", "")
    except httpx.HTTPError as exc:
        raise WatchError(f"the specification could not be fetched: {exc}", url=url) from exc

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WatchError("the specification is not valid UTF-8", url=url) from exc

    logger.debug("watch_fetched", url=url, bytes=len(body))
    return Fetched(
        text=text,
        etag=fetched_etag,
        last_modified=fetched_modified,
        source=url,
        suffix=_suffix_for(url),
    )


def fetch_file(path: Path | str, *, max_bytes: int = 32 * 1024 * 1024) -> Fetched:
    """Read a specification from disk.

    A vendored or generated specification is a legitimate thing to watch, and it
    costs nothing to poll. There are no cache validators, so change is detected
    by digest alone.

    Raises:
        WatchError: The file is missing, unreadable, oversized or not UTF-8.
    """
    file_path = Path(path).expanduser()
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        message = f"the specification could not be read: {exc}"
        raise WatchError(message, path=str(file_path)) from exc
    if size > max_bytes:
        raise WatchError(
            "the specification is larger than the safety limit",
            path=str(file_path),
            size_bytes=size,
            limit_bytes=max_bytes,
        )
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        message = f"the specification could not be read: {exc}"
        raise WatchError(message, path=str(file_path)) from exc
    return Fetched(text=text, source=str(file_path), suffix=_suffix_for(file_path.name))


def fetch(
    source: str,
    *,
    etag: str = "",
    last_modified: str = "",
    timeout_seconds: float = 30.0,
    max_bytes: int = 32 * 1024 * 1024,
    allow_http: bool = False,
) -> Fetched:
    """Read a watched specification, from wherever it lives.

    Raises:
        WatchError: The source could not be read. See :func:`fetch_url` and
            :func:`fetch_file`.
    """
    if is_url(source):
        return fetch_url(
            source,
            etag=etag,
            last_modified=last_modified,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            allow_http=allow_http,
        )
    return fetch_file(source, max_bytes=max_bytes)


__all__ = [
    "CHUNK_BYTES",
    "MAX_REDIRECTS",
    "USER_AGENT",
    "Fetched",
    "digest_of",
    "fetch",
    "fetch_file",
    "fetch_url",
    "is_url",
]
