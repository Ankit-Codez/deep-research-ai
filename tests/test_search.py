import hashlib
import json

import httpx
import pytest

from deepresearch_ai.config import ConfigurationError, Settings
from deepresearch_ai.models import SourceDocument
from deepresearch_ai.services.search import (
    SearchError, TavilyWebSearch, create_web_search, deduplicate_sources,
    normalize_search_results,
)
from deepresearch_ai.services.urls import UnsafeURL, canonical_url, public_ip, validate_web_url


def result(url="https://example.com/study", **overrides):
    return {"url": url, "title": "A primary study", "content": "The study presents its methods and results.", **overrides}


def test_search_normalization_preserves_original_url_and_stable_id():
    url = "https://EXAMPLE.com:443/study?utm_source=demo#results"
    sources = normalize_search_results([result(url)], max_content_chars=20)
    assert sources[0].original_url == url
    assert sources[0].source_id == "src_" + hashlib.sha256(url.encode()).hexdigest()
    assert sources[0].content == "The study presents i"


def test_search_deduplicates_tracking_fragments_case_and_standard_port():
    urls = [
        "https://EXAMPLE.com:443/study?id=1&utm_source=demo#results",
        "https://example.com/study?id=1#other",
        "https://example.com/study?id=2",
    ]
    sources = normalize_search_results([result(url) for url in urls], max_content_chars=500)
    assert [source.original_url for source in sources] == [urls[0], urls[2]]


def test_canonicalization_keeps_meaningful_url_differences():
    assert canonical_url("https://example.com") == "https://example.com/"
    assert canonical_url("https://example.com/a") != canonical_url("https://example.com/a/")
    assert canonical_url("https://example.com/A") != canonical_url("https://example.com/a")
    assert canonical_url("https://example.com/?a=1&a=2") != canonical_url("https://example.com/?a=2&a=1")


@pytest.mark.parametrize("bad", [
    None, "invalid", {}, result(title=""), result(content=""), result(url="file:///etc/passwd"),
    result(url="http://127.0.0.1/"), result(url="http://localhost/"), result(url="http://metadata.google.internal/"),
])
def test_search_ignores_invalid_or_private_results(bad):
    assert normalize_search_results([bad], max_content_chars=500) == []


def test_sources_with_same_final_redirect_target_are_deduplicated():
    sources = [SourceDocument(
        original_url=url, resolved_url="https://example.com/final", title="Title", content="Text",
    ) for url in ("https://example.com/one", "https://example.com/two")]
    assert deduplicate_sources(sources) == sources[:1]


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/a", "http://127.0.0.1/",
    "http://10.0.0.1/", "http://172.16.2.3/", "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data", "http://100.64.0.1/",
    "http://[::1]/", "http://[fd00::1]/", "http://[::ffff:127.0.0.1]/",
    "http://localhost/", "http://server/", "http://server.local/", "http://server.internal/",
    "https://user:password@example.com/", "https://example.com:8443/",
    "https://example.com/\nHeader", "https://example.com\\@127.0.0.1/",
])
def test_url_safety_rejects_unsafe_urls(url):
    with pytest.raises(UnsafeURL):
        validate_web_url(url)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
    "224.0.0.1", "::1", "fe80::1", "fd00::1", "::ffff:127.0.0.1",
    "64:ff9b::7f00:1", "2002:7f00:1::", "not-an-ip",
])
def test_non_public_and_transition_ips_are_blocked(address):
    assert not public_ip(address)


def test_tavily_request_contract_and_normalization():
    def handler(request):
        assert str(request.url) == "https://api.tavily.com/search"
        assert request.headers["authorization"] == "Bearer test-only-key"
        assert json.loads(request.content) == {
            "query": "RAG evidence", "max_results": 5, "search_depth": "basic",
            "include_answer": False, "include_raw_content": False,
        }
        return httpx.Response(200, json={"results": [result()]})

    provider = TavilyWebSearch(Settings(tavily_api_key="test-only-key"), transport=httpx.MockTransport(handler))
    assert provider.search("RAG evidence")[0].original_url == "https://example.com/study"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
def test_tavily_failures_are_sanitized_and_redirects_are_not_followed(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"location": "http://127.0.0.1/"}, text="secret-provider-body")

    provider = TavilyWebSearch(Settings(tavily_api_key="test-only-key"), transport=httpx.MockTransport(handler))
    with pytest.raises(SearchError, match=f"HTTP {status}") as error:
        provider.search("RAG evidence")
    assert "secret-provider-body" not in str(error.value)
    assert len(requests) == 1


def test_tavily_timeout_and_empty_results():
    def timeout(request):
        raise httpx.ReadTimeout("sensitive-provider-detail", request=request)

    settings = Settings(tavily_api_key="test-only-key")
    with pytest.raises(SearchError, match="timed out"):
        TavilyWebSearch(settings, transport=httpx.MockTransport(timeout)).search("RAG evidence")
    empty = httpx.MockTransport(lambda request: httpx.Response(200, json={"results": []}))
    assert TavilyWebSearch(settings, transport=empty).search("RAG evidence") == []


@pytest.mark.parametrize("payload", [b"not-json", b"[]", b'{"results":{}}'])
def test_tavily_malformed_payload(payload):
    provider = TavilyWebSearch(
        Settings(tavily_api_key="test-only-key"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload)),
    )
    with pytest.raises(SearchError):
        provider.search("RAG evidence")


def test_tavily_rejects_oversized_response():
    provider = TavilyWebSearch(
        Settings(tavily_api_key="test-only-key", research_max_response_bytes=1024),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 2048)),
    )
    with pytest.raises(SearchError, match="size limit"):
        provider.search("RAG evidence")


def test_missing_tavily_key_is_clear():
    with pytest.raises(ConfigurationError, match="Missing TAVILY_API_KEY"):
        create_web_search(Settings(mock_mode=False))
