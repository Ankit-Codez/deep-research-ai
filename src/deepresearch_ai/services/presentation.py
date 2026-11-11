"""Presentation boundary: no raw HTML, remote images, or arbitrary report links."""
import re

import bleach
from bs4 import BeautifulSoup, NavigableString
from markdown_it import MarkdownIt

from deepresearch_ai.models import ResearchReport
from deepresearch_ai.services.reporting import escape_markdown, display_title
from deepresearch_ai.services.urls import UnsafeURL, validate_web_url

TAGS = {'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'blockquote',
        'strong', 'em', 'code', 'pre', 'a', 'hr', 'br'}


def safe_link(url: str) -> str | None:
    """Validate public HTTP(S) syntax without making a DNS or network request."""
    try:
        return str(validate_web_url(url))
    except (UnsafeURL, TypeError):
        return None


def report_html(report: ResearchReport) -> str:
    allowed = {safe_link(url) for source in report.sources
               for url in (source.original_url, source.resolved_url) if url}
    allowed.discard(None)
    rendered = MarkdownIt('commonmark', {'html': False}).render(report.body_markdown)

    def attribute(tag, name, value):
        return tag == 'a' and name == 'href' and safe_link(value) in allowed

    return bleach.clean(rendered, tags=TAGS, attributes=attribute,
                        protocols={'http', 'https'}, strip=True, strip_comments=True)


def report_markdown(report: ResearchReport) -> str:
    """Export the same sanitized content as the UI, then append recorded citations.

    Rebuild Markdown from a small sanitized HTML subset rather than downloading
    raw model Markdown that could contain image tracking or unsafe links.
    """
    soup = BeautifulSoup(report_html(report), 'html.parser')

    def render(node):
        if isinstance(node, NavigableString):
            return escape_markdown(str(node)) if str(node).strip() else ' '
        text = ''.join(render(child) for child in node.children)
        name = node.name
        if name == 'a' and node.get('href'):
            # Angle destinations keep parentheses in valid source URLs unambiguous.
            return f'[{text}](<{safe_link(node["href"])}>)'
        if name in {'strong', 'em'}:
            delimiter = '**' if name == 'strong' else '*'
            return delimiter + text + delimiter
        if name and re.fullmatch(r'h[1-6]', name):
            return '\n\n' + '#' * int(name[1]) + ' ' + text.strip() + '\n\n'
        if name == 'li':
            return '\n- ' + text.strip() + '\n'
        if name == 'blockquote':
            return '\n\n' + '\n'.join('> ' + line for line in text.strip().splitlines()) + '\n\n'
        if name in {'p', 'pre', 'ul', 'ol'}:
            return '\n\n' + text.strip() + '\n\n'
        if name in {'br', 'hr'}:
            return '\n\n'
        return text

    body = re.sub(r'\n{3,}', '\n\n', ''.join(render(node) for node in soup.contents)).strip()
    lines = ['# ' + escape_markdown(report.title), '', body, '', '## Sources', '']
    sources = {source.source_id: source for source in report.sources}
    for label, source_id in report.citations.items():
        source = sources[source_id]
        url = safe_link(source.original_url)
        title = escape_markdown(display_title(source.title))
        lines.append(f'- \\[{label}\\] [{title}](<{url}>)' if url else f'- \\[{label}\\] {title} (unsafe URL omitted)')
        lines.append(f'  Published: {source.published_at or "unknown"}; modified: {source.modified_at or "unknown"}; retrieved: {source.retrieved_at.isoformat()}')
    return '\n'.join(lines) + '\n'
