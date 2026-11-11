"""Search providers normalize metadata to the existing source-document contract."""

import json
from typing import Protocol

import httpx
from pydantic import ValidationError

from deepresearch_ai.config import ConfigurationError, Settings
from deepresearch_ai.services.runtime import remaining_seconds, check_deadline
from deepresearch_ai.models import ResearchRequest, SourceDocument
from deepresearch_ai.services.mock_data import sample_sources
from deepresearch_ai.services.urls import UnsafeURL, canonical_url, validate_web_url


class SearchError(RuntimeError):
    """A sanitized provider failure, safe to include in report limitations."""

    def __init__(self, message, *, transient=False):
        super().__init__(message)
        self.transient = transient


class WebSearchProvider(Protocol):
    """Search metadata; snippets are not treated as retrieved page evidence."""

    def search(self, query: str, *, max_results: int = 5) -> list[SourceDocument]: ...


WebSearch = WebSearchProvider  # Preserve the Phase 1 import name.


def deduplicate_sources(sources: list[SourceDocument]) -> list[SourceDocument]:
    unique: dict[str, SourceDocument] = {}
    for source in sources:
        try:
            key = canonical_url(source.resolved_url or source.original_url)
        except UnsafeURL:
            continue
        unique.setdefault(key, source)
    return list(unique.values())


def normalize_search_results(results: list[object], *, max_content_chars: int) -> list[SourceDocument]:
    normalized = []
    for result in results:
        if not isinstance(result, dict):
            continue
        url, title, content = (result.get(key) for key in ("url", "title", "content"))
        if not all(isinstance(value, str) and value.strip() for value in (url, title, content)):
            continue
        try:
            validate_web_url(url)
            normalized.append(SourceDocument(
                original_url=url, title=title[:500], content=content[:max_content_chars],
            ))
        except (UnsafeURL, ValidationError):
            continue
    return deduplicate_sources(normalized)


class MockWebSearch:
    """Always return the fixed RAG sample, regardless of the supplied query."""

    def search(self, query: str, *, max_results: int = 5) -> list[SourceDocument]:
        request = ResearchRequest(query=query, max_sources=max_results)
        return sample_sources()[:request.max_sources]


class TavilyWebSearch:
    """Use Tavily's documented REST endpoint without an additional SDK."""

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        if settings.tavily_api_key is None or not settings.tavily_api_key.get_secret_value().strip():
            raise ConfigurationError("Missing TAVILY_API_KEY. Set it in the environment or .env.")
        self.settings = settings
        self.transport = transport

    def search(self, query: str, *, max_results: int = 5) -> list[SourceDocument]:
        ResearchRequest(query=query, max_sources=max_results)
        limit = min(max_results, 10)
        try:
            with httpx.Client(
                transport=self.transport, trust_env=False, follow_redirects=False,
                timeout=min(self.settings.research_request_timeout, remaining_seconds()),
            ) as client:
                with client.stream(
                    "POST", "https://api.tavily.com/search",
                    headers={"Authorization": f"Bearer {self.settings.tavily_api_key.get_secret_value()}"},
                    json={"query": query, "max_results": limit, "search_depth": "basic",
                          "include_answer": False, "include_raw_content": False},
                ) as response:
                    if response.status_code != 200:
                        raise SearchError(f"Search provider returned HTTP {response.status_code}.",
                                          transient=response.status_code in {408, 429, 500, 502, 503, 504})
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=16384):
                        check_deadline()
                        body.extend(chunk)
                        if len(body) > self.settings.research_max_response_bytes:
                            raise SearchError("Search provider response exceeded the size limit.")
            data = json.loads(body)
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise SearchError("Search provider returned an invalid results payload.")
            return normalize_search_results(
                data["results"][:limit], max_content_chars=self.settings.research_max_content_chars,
            )
        except httpx.TimeoutException as exc:
            raise SearchError("Search provider timed out.", transient=True) from exc
        except httpx.HTTPError as exc:
            raise SearchError("Search provider could not be reached.", transient=True) from exc
        except (ValueError, UnicodeError) as exc:
            raise SearchError("Search provider returned invalid JSON.") from exc


def create_web_search(settings: Settings | None = None) -> WebSearchProvider:
    """Select mocks before checking keys or touching any real search integration."""
    settings = settings if settings is not None else Settings()
    if settings.mock_mode:
        return MockWebSearch()
    return TavilyWebSearch(settings)
