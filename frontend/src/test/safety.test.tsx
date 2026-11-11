import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SafeMarkdown } from '../components/ReportView';
import { safeSourceURL } from '../safety';
import { makeRun } from './fixtures';

describe('source and Markdown safety', () => {
  it.each(['javascript:alert(1)', 'data:text/html,evil', 'file:///secret', '//example.com/page',
    'http://localhost/secret', 'http://127.0.0.1/secret', 'http://[::1]/secret',
    'http://2130706433/secret', 'https://office.internal/page', 'https://site.local./page',
    'https://user:password@example.com', 'https://example.com:8443/page', 'https://example.com/\nunsafe',
    'https://example.com\\private'])('blocks unsafe source URL %s', value => {
    expect(safeSourceURL(value)).toBeNull();
  });
  it('accepts normal source links and drops fragments', () => {
    expect(safeSourceURL('https://example.com/paper#section')).toBe('https://example.com/paper');
  });
  it('renders only recorded source links, without HTML or remote images', () => {
    const report = makeRun().report!;
    report.body_markdown = `<script>alert(1)</script>\n\n![tracking](https://evil.example/pixel.png)\n\n[fictional](https://evil.example/fake)\n\n[private](http://127.0.0.1/secret)\n\n[actual source](${report.sources[0].original_url})\n\n> A readable passage. [1]`;
    const { container } = render(<SafeMarkdown report={report} />);
    expect(container.querySelector('script, iframe, img')).toBeNull();
    expect(screen.getAllByRole('link')).toHaveLength(1);
    expect(screen.getByRole('link', { name: 'actual source' })).toHaveAttribute('href', report.sources[0].original_url);
    expect(screen.getByText('A readable passage. [1]')).toBeInTheDocument();
    expect(screen.getByText('fictional')).not.toHaveAttribute('href');
  });
});
