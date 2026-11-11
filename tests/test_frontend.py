"""Server-side report sanitization shared by React downloads and API clients."""
import pytest
from bs4 import BeautifulSoup
from deepresearch_ai.services.mock_data import sample_report
from deepresearch_ai.services.presentation import report_html, report_markdown, safe_link


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'data:text/html,evil', 'file:///secret',
    'http://127.0.0.1/secret', 'http://localhost/private', 'https://user:password@example.com',
    'https://example.com:8443/page', '//example.com/page', 'https://example.com/\nunsafe'])
def test_unsafe_links_are_not_clickable(url):
    assert safe_link(url) is None


def test_rendered_html_and_download_remove_unsafe_content_and_external_images():
    report = sample_report()
    source_url = report.sources[0].original_url
    report = report.model_copy(update={'body_markdown': (
        '# Results\n\n<script>alert(1)</script>\n\n'
        '<iframe src="https://attacker.example/track"></iframe>\n\n'
        '![tracker](https://attacker.example/track.png)\n\n'
        '[invented](https://attacker.example/fiction)\n\n'
        '[private](http://127.0.0.1/secret)\n\n'
        '[bad](javascript:alert(1))\n\n'
        f'[actual source]({source_url})\n\n> A readable passage. [1]')})
    html = report_html(report)
    soup = BeautifulSoup(html, 'html.parser')
    assert not soup.find(['script', 'iframe', 'img'])
    links = [node['href'] for node in soup.find_all('a', href=True)]
    assert links == [source_url]
    assert 'onerror=' not in html
    downloaded = report_markdown(report)
    assert '<script>' not in downloaded and '<iframe' not in downloaded
    assert '](<https://attacker.example' not in downloaded
    assert '![tracker]' not in downloaded
    assert '## Sources' in downloaded and source_url in downloaded
    assert 'A readable passage' in downloaded


def test_markdown_download_includes_source_dates_and_citation_labels():
    text = report_markdown(sample_report())
    assert 'retrieved:' in text and '\\[1\\]' in text
    assert sample_report().sources[0].original_url in text
