import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { ReportView } from '../components/ReportView';
import { displaySourceTitle } from '../safety';
import { makeRun, RUN_ID } from './fixtures';

describe('report quality and evidence presentation', () => {
  it('shows a partial answer distinctly from failed evidence checks', () => {
    const report = makeRun().report!;
    report.status = 'partial';
    render(<ReportView report={report} sources={report.sources} runId={RUN_ID} />);
    expect(screen.getByText('Supported answer with limitations')).toBeInTheDocument();
    expect(screen.getByText(/Some requested details or claims remain unresolved/)).toBeInTheDocument();
    expect(screen.queryByText('Evidence needs attention')).not.toBeInTheDocument();
    expect(screen.queryByText('Evidence checks passed')).not.toBeInTheDocument();
  });

  it('decodes source titles as plain text and gives references real recorded URLs', async () => {
    const report = makeRun().report!;
    report.sources[0].title = '&#x53;ource &amp; methods &#110; &lt;script&gt;alert(1)&lt;/script&gt;';
    const { container } = render(<ReportView report={report} sources={report.sources} runId={RUN_ID} />);
    const reference = screen.getByRole('link', { name: /Source & methods n/ });
    expect(reference).toHaveAttribute('href', report.sources[0].original_url);
    expect(reference).toHaveAttribute('rel', 'noopener noreferrer');
    expect(reference).toHaveAttribute('target', '_blank');
    expect(container.querySelector('script, img, iframe')).toBeNull();
    expect(container.textContent).not.toContain('&#x53;');
    await userEvent.click(screen.getByRole('tab', { name: /Sources/ }));
    expect(screen.getByRole('link', { name: /Source & methods n/ })).toHaveAttribute('href', report.sources[0].original_url);
  });

  it('does not turn unsafe recorded source URLs into reference links', () => {
    const report = makeRun().report!;
    report.sources[0].original_url = 'javascript:alert(1)';
    const { container } = render(<ReportView report={report} sources={report.sources} runId={RUN_ID} />);
    expect(screen.getByText(/unsafe URL omitted/)).toBeInTheDocument();
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
  });

  it('labels passage judgments so irrelevant passages are not presented as support', async () => {
    const report = makeRun().report!;
    report.claims[0].passage_assessments = [{ evidence_id: report.claims[0].evidence_ids[0],
      relation: 'irrelevant', explanation: 'This passage does not address this claim.' }];
    render(<ReportView report={report} sources={report.sources} runId={RUN_ID} />);
    await userEvent.click(screen.getByRole('tab', { name: 'Evidence' }));
    expect(screen.getByText('This passage does not address this claim.').closest('blockquote'))
      .toHaveTextContent('Not relevant to this claim');
  });

  it('decodes nested numeric entities and removes misleading control characters', () => {
    expect(displaySourceTitle('&amp;#x53;ource&#x202e; &#x1f4da;')).toBe('Source 📚');
    expect(displaySourceTitle('Bad &#x110000; &#55296;')).toBe('Bad � �');
  });
});
