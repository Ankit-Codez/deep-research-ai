from unittest.mock import Mock

import httpx
import pytest

from deepresearch_ai.config import Settings
from deepresearch_ai.models import SourceDocument
from deepresearch_ai.services.retrieval import PublicNetworkTransport, RetrievalError, SafeSourceRetriever
from deepresearch_ai.services.urls import UnsafeURL

PUBLIC_IP = "93.184.215.14"
TEXT = "A primary source describes retrieval and provides external evidence for generation. " * 4


def response(status=200, body=TEXT, **headers):
    return httpx.Response(
        status, headers={"content-type": "text/html", **headers},
        stream=httpx.ByteStream(body.encode()),
    )


def source(url="https://example.com/study"):
    return SourceDocument(original_url=url, title="Search title", content="Search snippet must be replaced.")


def retriever(handler, resolver=None, **settings):
    return SafeSourceRetriever(
        Settings(**{"research_request_delay": 0, **settings}),
        transport=PublicNetworkTransport(
            inner=httpx.MockTransport(handler), resolver=resolver or (lambda host, port: [PUBLIC_IP]),
        ),
    )


def test_fetch_extracts_visible_text_preserves_identity_and_ignores_canonical_tag():
    def handler(request):
        if request.url.path == "/robots.txt":
            return response(404, "")
        return response(body=(
            "<html><head><title>Actual page title</title>"
            '<link rel="canonical" href="http://127.0.0.1/internal"></head>'
            f"<body><script>hidden injection</script><nav>Menu</nav><main>{TEXT}</main></body></html>"
        ))

    candidate = source()
    with retriever(handler) as fetcher:
        document = fetcher.fetch(candidate)
    assert document.source_id == candidate.source_id
    assert document.original_url == candidate.original_url
    assert document.resolved_url == candidate.original_url
    assert document.title == "Actual page title"
    assert "hidden injection" not in document.content
    assert "Menu" not in document.content
    assert "Search snippet" not in document.content


def test_connection_pins_checked_ip_and_preserves_host_tls_name_and_timeout():
    resolver = Mock(return_value=[PUBLIC_IP])

    def handler(request):
        assert request.url.host == PUBLIC_IP
        assert request.headers["host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        assert request.extensions["timeout"]["connect"] == 2
        return response()

    with httpx.Client(transport=PublicNetworkTransport(
        inner=httpx.MockTransport(handler), resolver=resolver,
    ), trust_env=False, timeout=2) as client:
        client.get("https://example.com/study")
    resolver.assert_called_once_with("example.com", 443)


@pytest.mark.parametrize("addresses", [[], ["127.0.0.1"], [PUBLIC_IP, "10.0.0.1"], ["::1"]])
def test_dns_non_public_or_mixed_answers_never_connect(addresses):
    handler = Mock(side_effect=AssertionError("Must not connect"))
    transport = PublicNetworkTransport(inner=httpx.MockTransport(handler), resolver=lambda *args: addresses)
    with httpx.Client(transport=transport, trust_env=False) as client:
        with pytest.raises(UnsafeURL):
            client.get("https://example.com/study")
    handler.assert_not_called()


def test_obfuscated_numeric_host_is_blocked_after_resolution():
    handler = Mock(side_effect=AssertionError("Must not connect"))
    transport = PublicNetworkTransport(inner=httpx.MockTransport(handler), resolver=lambda *args: ["127.0.0.1"])
    with httpx.Client(transport=transport, trust_env=False) as client:
        with pytest.raises(UnsafeURL):
            client.get("http://127.1/")
    handler.assert_not_called()


def test_dns_rebinding_is_not_given_a_second_hostname_lookup():
    resolver = Mock(side_effect=[[PUBLIC_IP], ["127.0.0.1"]])
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return response()

    with httpx.Client(transport=PublicNetworkTransport(
        inner=httpx.MockTransport(handler), resolver=resolver,
    ), trust_env=False) as client:
        client.get("https://example.com/a")
        with pytest.raises(UnsafeURL):
            client.get("https://example.com/b")
    assert seen == [PUBLIC_IP]
    assert resolver.call_count == 2


@pytest.mark.parametrize("destination", ["http://127.0.0.1/private", "http://169.254.169.254/meta", "https://server.local/"])
def test_redirect_to_private_address_is_never_followed(destination):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return response(404, "")
        return response(302, "", location=destination)

    with retriever(handler) as fetcher:
        with pytest.raises(RetrievalError):
            fetcher.fetch(source())
    assert paths == ["/robots.txt", "/study"]


def test_redirect_to_hostname_resolving_private_is_never_connected():
    seen = []

    def handler(request):
        seen.append(request.headers["host"])
        return response(404, "") if request.url.path == "/robots.txt" else response(302, "", location="https://attacker.example/page")

    resolver = lambda host, port: ["127.0.0.1"] if host == "attacker.example" else [PUBLIC_IP]
    with retriever(handler, resolver=resolver) as fetcher:
        with pytest.raises(RetrievalError):
            fetcher.fetch(source())
    assert seen == ["example.com", "example.com"]


def test_redirect_target_must_allow_robots_and_records_provenance():
    def handler(request):
        if request.url.path == "/robots.txt":
            return response(404, "")
        if request.url.path == "/study":
            return response(302, "", location="https://other.example/final")
        return response()

    with retriever(handler) as fetcher:
        document = fetcher.fetch(source())
    assert document.original_url == "https://example.com/study"
    assert document.resolved_url == "https://other.example/final"


@pytest.mark.parametrize("robots_status", [401, 403, 429, 500])
def test_robots_failures_block_page_access(robots_status):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return response(robots_status, "")

    with retriever(handler) as fetcher:
        with pytest.raises(RetrievalError, match="robots.txt"):
            fetcher.fetch(source())
    assert paths == ["/robots.txt"]


def test_robots_disallow_is_respected():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return response(body="User-agent: *\nDisallow: /study\n")

    with retriever(handler) as fetcher:
        with pytest.raises(RetrievalError, match="restricted by robots"):
            fetcher.fetch(source())
    assert paths == ["/robots.txt"]


def test_unreachable_or_timed_out_page_is_a_retrieval_error():
    def handler(request):
        if request.url.path == "/robots.txt":
            return response(404, "")
        raise httpx.ReadTimeout("sensitive-network-detail", request=request)

    with retriever(handler) as fetcher:
        with pytest.raises(RetrievalError, match="ReadTimeout") as error:
            fetcher.fetch(source())
    assert "sensitive-network-detail" not in str(error.value)


@pytest.mark.parametrize("status", [401, 403, 404, 429, 503])
def test_blocked_or_error_pages_are_skipped(status):
    handler = lambda request: response(404, "") if request.url.path == "/robots.txt" else response(status)
    with retriever(handler) as fetcher:
        with pytest.raises(RetrievalError, match=f"HTTP {status}"):
            fetcher.fetch(source())


@pytest.mark.parametrize(("body", "headers"), [
    ("tiny", {}), (TEXT, {"content-type": "application/pdf"}),
    (TEXT, {"x-robots-tag": "noindex, nosnippet"}),
    (f'<meta name="robots" content="noai">{TEXT}', {}),
    (f"<title>Just a moment...</title>{TEXT}", {}),
])
def test_unusable_or_restricted_content_is_skipped(body, headers):
    handler = lambda request: response(404, "") if request.url.path == "/robots.txt" else response(body=body, **headers)
    with retriever(handler) as fetcher:
        with pytest.raises(RetrievalError):
            fetcher.fetch(source())


def test_download_and_extracted_text_limits():
    handler = lambda request: response(404, "") if request.url.path == "/robots.txt" else response(body=TEXT * 20)
    with retriever(handler, research_max_response_bytes=1024) as fetcher:
        with pytest.raises(RetrievalError, match="size limit"):
            fetcher.fetch(source())
    with retriever(handler, research_max_content_chars=200) as fetcher:
        assert len(fetcher.fetch(source()).content) == 200


def test_redirect_limit_prevents_loops():
    handler = lambda request: response(404, "") if request.url.path == "/robots.txt" else response(302, "", location="/study")
    with retriever(handler, research_max_redirects=1) as fetcher:
        with pytest.raises(RetrievalError, match="Redirect limit"):
            fetcher.fetch(source())


def test_redirect_destination_robots_rules_are_checked_before_fetch():
    seen = []

    def handler(request):
        seen.append((request.headers["host"], request.url.path))
        if request.url.path == "/robots.txt":
            return response(body="User-agent: *\nDisallow: /final") if request.headers["host"] == "other.example" else response(404, "")
        return response(302, "", location="https://other.example/final")

    with retriever(handler) as fetcher:
        with pytest.raises(RetrievalError, match="restricted by robots"):
            fetcher.fetch(source())
    assert seen == [("example.com", "/robots.txt"), ("example.com", "/study"), ("other.example", "/robots.txt")]


def test_https_downgrades_are_refused():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return response(404, "") if request.url.path == "/robots.txt" else response(302, "", location="http://other.example/final")

    with retriever(handler) as fetcher:
        with pytest.raises(RetrievalError, match="downgrade"):
            fetcher.fetch(source())
    assert seen == ["/robots.txt", "/study"]


def test_compressed_sources_are_refused_before_decompression():
    def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        if request.url.path == "/robots.txt":
            return response(404, "")
        return response(body="not-even-gzip", **{"content-encoding": "gzip"})

    with retriever(handler) as fetcher:
        with pytest.raises(RetrievalError, match="Compressed responses"):
            fetcher.fetch(source())


def test_crawl_delay_and_robots_cache_are_respected(monkeypatch):
    from deepresearch_ai.services import retrieval

    clock = [100.0]
    seen = []
    monkeypatch.setattr(retrieval.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(retrieval.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))

    def handler(request):
        seen.append((request.url.path, clock[0]))
        return response(body="User-agent: *\nCrawl-delay: 2\n") if request.url.path == "/robots.txt" else response()

    with retriever(handler, research_request_delay=1) as fetcher:
        fetcher.fetch(source())
        fetcher.fetch(source("https://example.com/second"))
    assert seen == [("/robots.txt", 100.0), ("/study", 102.0), ("/second", 104.0)]


def test_page_deadline_prevents_waiting_beyond_its_budget(monkeypatch):
    from deepresearch_ai.services import retrieval

    monkeypatch.setattr(retrieval.time, "monotonic", lambda: 100.0)
    handler = lambda request: response(body="User-agent: *\nCrawl-delay: 5\n")
    with retriever(handler, research_page_deadline=1) as fetcher:
        with pytest.raises(RetrievalError, match="deadline"):
            fetcher.fetch(source())


def test_slow_stream_exceeding_deadline_is_stopped(monkeypatch):
    from deepresearch_ai.services import retrieval

    clock = [100.0]
    monkeypatch.setattr(retrieval.time, "monotonic", lambda: clock[0])

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            clock[0] += 2
            yield TEXT.encode()

    def handler(request):
        if request.url.path == "/robots.txt":
            return response(404, "")
        return httpx.Response(200, headers={"content-type": "text/plain"}, stream=SlowStream())

    with retriever(handler, research_page_deadline=1) as fetcher:
        with pytest.raises(RetrievalError, match="deadline"):
            fetcher.fetch(source())
