"""Bounded public-web retrieval, robots checks, and a DNS-pinning transport."""

import socket
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Protocol, Self
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from deepresearch_ai.config import Settings
from deepresearch_ai.services.runtime import remaining_seconds
from deepresearch_ai.models import SourceDocument
from deepresearch_ai.services.mock_data import sample_sources
from deepresearch_ai.services.urls import UnsafeURL, canonical_url, public_ip, validate_web_url

USER_AGENT = "DeepResearchAI/0.2"


class RetrievalError(RuntimeError):
    """A sanitized retrieval failure; raw response bodies are never logged."""


def resolve_addresses(host: str, port: int) -> list[str]:
    return list(dict.fromkeys(
        item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ))


class PublicNetworkTransport(httpx.BaseTransport):
    """Connect to a checked numeric IP while preserving HTTP Host and TLS SNI.

    HTTPX must not re-resolve an attacker-controlled hostname after validation.
    No proxies, keep-alive cross-host reuse, or connection retries are enabled.
    The inner transport/resolver are injectable for network-free security tests.
    """

    def __init__(
        self, *, inner: httpx.BaseTransport | None = None,
        resolver: Callable[[str, int], list[str]] = resolve_addresses,
    ):
        self.inner = inner if inner is not None else httpx.HTTPTransport(
            trust_env=False, retries=0,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        )
        self.resolver = resolver

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = validate_web_url(str(request.url))
        host = url.raw_host.decode("ascii").rstrip(".")
        port = url.port or (443 if url.scheme == "https" else 80)
        try:
            addresses = self.resolver(host, port)
        except OSError as exc:
            raise RetrievalError("DNS resolution failed.") from exc
        if not addresses or not all(public_ip(address) for address in addresses):
            raise UnsafeURL("DNS returned a non-public or invalid address.")
        pinned = httpx.Request(
            request.method, url.copy_with(host=addresses[0]),
            headers=request.headers, stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        return self.inner.handle_request(pinned)

    def close(self) -> None:
        self.inner.close()


class SourceRetriever(Protocol):
    def __enter__(self) -> Self: ...
    def __exit__(self, *args: object) -> None: ...
    def fetch(self, source: SourceDocument) -> SourceDocument: ...


class SafeSourceRetriever:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.client = httpx.Client(
            transport=transport if transport is not None else PublicNetworkTransport(),
            trust_env=False, follow_redirects=False,
            timeout=settings.research_request_timeout,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity",
                     "Accept": "text/html, text/plain;q=0.9"},
        )
        self.robots: dict[str, RobotFileParser | None] = {}
        self.last_request: dict[str, float] = {}
        self.delays: dict[str, float] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.client.close()

    def _read(self, url: str, deadline: float, limit: int) -> tuple[int, httpx.Headers, bytes]:
        parsed = validate_web_url(url)
        origin = str(parsed.copy_with(path="/", query=None, fragment=None)).rstrip("/")
        delay = self.delays.get(origin, self.settings.research_request_delay)
        pause = max(0, self.last_request.get(origin, 0) + delay - time.monotonic())
        if time.monotonic() + pause >= deadline:
            raise RetrievalError("Page deadline exceeded.")
        if pause:
            time.sleep(pause)
        self.last_request[origin] = time.monotonic()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RetrievalError("Page deadline exceeded.")
        with self.client.stream(
            "GET", parsed, timeout=min(remaining, self.settings.research_request_timeout),
        ) as response:
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise RetrievalError("Compressed responses are not supported by the bounded reader.")
            body = bytearray()
            for chunk in response.iter_raw(chunk_size=16384):
                if time.monotonic() > deadline:
                    raise RetrievalError("Page deadline exceeded.")
                body.extend(chunk)
                if len(body) > limit:
                    raise RetrievalError("Response exceeded the download size limit.")
            return response.status_code, response.headers, bytes(body)

    def _download(
        self, url: str, deadline: float, *, robots_file: bool = False,
    ) -> tuple[str, int, httpx.Headers, bytes]:
        current = str(validate_web_url(url))
        for hop in range(self.settings.research_max_redirects + 1):
            if not robots_file:
                self._check_robots(current, deadline)
            limit = 50_000 if robots_file else self.settings.research_max_response_bytes
            status, headers, body = self._read(current, deadline, limit)
            if status in {301, 302, 303, 307, 308}:
                if hop == self.settings.research_max_redirects or not headers.get("location"):
                    raise RetrievalError("Redirect limit reached or Location missing.")
                next_url = str(validate_web_url(urljoin(current, headers["location"])))
                if current.startswith("https:") and next_url.startswith("http:"):
                    raise RetrievalError("HTTPS downgrade redirect blocked.")
                current = next_url
                continue
            return current, status, headers, body
        raise RetrievalError("Redirect limit reached.")

    def _check_robots(self, url: str, deadline: float) -> None:
        parsed = validate_web_url(url)
        origin = str(parsed.copy_with(path="/", query=None, fragment=None)).rstrip("/")
        if origin not in self.robots:
            self.robots[origin] = None  # Cache failures conservatively for this run.
            _, status, _, body = self._download(origin + "/robots.txt", deadline, robots_file=True)
            parser = RobotFileParser()
            if status == 404:
                parser.parse([])
            elif status == 200:
                parser.parse(body.decode("utf-8", errors="replace").splitlines())
            else:
                raise RetrievalError("robots.txt is unavailable or access is denied.")
            self.robots[origin] = parser
        parser = self.robots[origin]
        if parser is None or not parser.can_fetch(USER_AGENT, url):
            raise RetrievalError("Site access restricted by robots.txt.")
        delay = parser.crawl_delay(USER_AGENT) or 0
        rate = parser.request_rate(USER_AGENT)
        if rate and rate.requests > 0:
            delay = max(delay, rate.seconds / rate.requests)
        if delay > 10:
            raise RetrievalError("Site crawl delay exceeds this research run's limits.")
        self.delays[origin] = max(delay, self.settings.research_request_delay)

    def fetch(self, source: SourceDocument) -> SourceDocument:
        try:
            final_url, status, headers, raw = self._download(
                source.original_url, time.monotonic() + min(self.settings.research_page_deadline, remaining_seconds()),
            )
            if status != 200:
                raise RetrievalError(f"Source returned HTTP {status}.")
            kind = headers.get("content-type", "").split(";")[0].strip().lower()
            if kind not in {"text/html", "application/xhtml+xml", "text/plain"}:
                raise RetrievalError("Source is not supported HTML or plain text.")
            restrictions = headers.get("x-robots-tag", "").lower()
            if any(token in restrictions for token in ("noindex", "nosnippet", "noai", "none")):
                raise RetrievalError("Source requests restricted indexing or text use.")
            text = raw.decode("utf-8", errors="replace")
            title = source.title
            published_at, modified_at = source.published_at, source.modified_at
            if kind != "text/plain":
                soup = BeautifulSoup(raw, "html.parser")
                for key, field in (("article:published_time", "published"), ("article:modified_time", "modified"),
                                   ("datePublished", "published"), ("dateModified", "modified")):
                    meta = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
                    if meta and isinstance(meta.get("content"), str):
                        try:
                            recorded = date.fromisoformat(meta["content"][:10])
                        except ValueError:
                            continue
                        if field == "published":
                            published_at = recorded
                        else:
                            modified_at = recorded
                for meta in soup.find_all("meta", attrs={"name": True}):
                    if meta.get("name", "").lower() in {"robots", "deepresearchai"}:
                        value = str(meta.get("content", "")).lower()
                        if any(token in value for token in ("noindex", "nosnippet", "noai", "none")):
                            raise RetrievalError("Source requests restricted indexing or text use.")
                if soup.title and soup.title.get_text(strip=True):
                    title = soup.title.get_text(" ", strip=True)[:500]
                    if any(marker in title.lower() for marker in (
                        "just a moment", "access denied", "verify you are human", "captcha",
                    )):
                        raise RetrievalError("Source presents an access challenge.")
                for element in soup(["script", "style", "nav", "footer", "header", "form", "noscript", "template"]):
                    element.decompose()
                text = soup.get_text(" ", strip=True)
            text = " ".join(text.split())[:self.settings.research_max_content_chars]
            if len(text) < 80:
                raise RetrievalError("Source text is empty or too short to use as evidence.")
            # Keep the original search URL and its stable ID; record redirect provenance.
            return SourceDocument(
                original_url=source.original_url, resolved_url=final_url,
                title=title, content=text, retrieved_at=datetime.now(timezone.utc),
                published_at=published_at, modified_at=modified_at,
            )
        except (httpx.HTTPError, UnsafeURL, OSError) as exc:
            raise RetrievalError(f"Source retrieval failed ({type(exc).__name__}).") from exc


class MockSourceRetriever:
    """Retrieve only known local fixtures; never use a candidate's search snippet."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def fetch(self, source: SourceDocument) -> SourceDocument:
        for fixture in sample_sources():
            if canonical_url(source.original_url) == canonical_url(fixture.original_url):
                return SourceDocument(
                    original_url=source.original_url, title=fixture.title,
                    content=fixture.content[:self.settings.research_max_content_chars],
                    retrieved_at=fixture.retrieved_at, published_at=fixture.published_at, modified_at=fixture.modified_at,
                )
        raise RetrievalError("No local mock document is available for this source.")


def create_source_retriever(settings: Settings) -> SourceRetriever:
    if settings.mock_mode:
        return MockSourceRetriever(settings)
    return SafeSourceRetriever(settings)
