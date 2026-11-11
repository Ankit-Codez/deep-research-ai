"""Partial answers, scoped drafts, cited prose and safe reference presentation."""
from copy import deepcopy
from unittest.mock import Mock

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from markdown_it import MarkdownIt
import pytest

from deepresearch_ai.agents.evidence import EvidenceAgent
from deepresearch_ai.agents.writer import WriterAgent
from deepresearch_ai.api.app import create_app
from deepresearch_ai.config import Settings
from deepresearch_ai.models import ClaimVerification, ResearchReport, ResearchRequest
from deepresearch_ai.models.agents import ClaimNote, EvidenceCandidate, EvidenceExtraction, ResearchClaim
from deepresearch_ai.services.mock_data import SAMPLE_QUERY
from deepresearch_ai.services.presentation import report_html, report_markdown
from deepresearch_ai.services.workflow import GraphResearchService
from deepresearch_ai.storage.sql import SQLRunRepository
from deepresearch_ai.worker import ResearchWorker


@pytest.fixture
def state():
    return GraphResearchService(Settings(mock_mode=True)).run_state(ResearchRequest(query=SAMPLE_QUERY))


def add_gap(state, *, priority='high'):
    claim = ResearchClaim(claim_id='missing_detail', claim='A requested detail remains unverified.', priority=priority)
    state['claims'].append(claim)
    state['verification_results'].append(ClaimVerification(claim_id=claim.claim_id, claim=claim.claim,
        verdict='insufficient_evidence', rationale='The retrieved passages do not cover this detail.'))


def render(state):
    return WriterAgent(Settings(mock_mode=True))(state)['report']


def test_checked_multi_source_answer_with_core_gap_is_partial_and_retains_findings(state):
    add_gap(state)
    original = deepcopy(state)
    report = render(state)
    assert report.status == 'partial'
    assert len(report.citations) == 2
    assert 'Supported answer with limitations' in report.body_markdown
    assert '## Unresolved claims' in report.body_markdown
    findings = report.body_markdown.split('## Unresolved claims')[0]
    assert 'requested detail remains unverified' not in findings
    assert not any(line.startswith('> ') for line in findings.splitlines())
    text = BeautifulSoup(report_html(report), 'html.parser').get_text(' ')
    assert all(claim.claim in text for claim in state['claims'][:2])
    assert state == original
    assert ResearchReport.model_validate_json(report.model_dump_json()) == report


def test_optional_gaps_are_separate_from_core_findings(state):
    add_gap(state, priority='normal')
    report = render(state)
    assert report.status == 'partial'
    assert '## Optional gaps' in report.body_markdown
    assert '## Unresolved claims' not in report.body_markdown


def test_incomplete_coverage_with_supported_findings_is_partial(state):
    state['coverage_sufficient'] = False
    assert render(state).status == 'partial'


@pytest.mark.parametrize('failure', ['provider', 'budget', 'rejected', 'forged_matrix'])
def test_partial_status_does_not_hide_failed_checks_or_forged_judgments(state, failure):
    if failure == 'provider':
        state['step_status']['verification'] = 'failed'
    elif failure == 'budget':
        state['usage'].limit_reason = 'Research token-reservation limit reached.'
    elif failure == 'rejected':
        state['rejected_evidence'] = True
    else:
        result = state['verification_results'][0]
        result.passage_assessments = [item for item in result.passage_assessments if item.relation == 'supports']
        result.evidence_ids = [item.evidence_id for item in result.passage_assessments]
    assert render(state).status == 'insufficient_evidence'


def test_context_examples_are_scoped_and_cannot_alone_answer_the_question(state):
    for claim in state['claims']:
        claim.relevance = 'context'
        claim.priority = 'normal'
    report = render(state)
    assert report.status == 'insufficient_evidence'
    assert '## Source-specific examples' in report.body_markdown and '## Findings' not in report.body_markdown
    assert any('do not establish universal practice' in message for message in report.uncertainties)


def test_off_topic_candidates_and_notes_are_excluded_without_poisoning_evidence(state):
    source = state['sources'][0]
    state['claims'], state['evidence'] = [], []
    output = EvidenceExtraction(coverage_sufficient=True, candidates=[
        EvidenceCandidate(claim='A scoped example.', source_id=source.source_id,
                          excerpt=source.content, relevance='context', priority='high'),
        EvidenceCandidate(claim='An unrequested side assertion.', source_id=source.source_id,
                          excerpt=source.content, relevance='off_topic')],
        notes=[ClaimNote(claim='An invented niche attack needs proof.', relevance='off_topic')])
    model = Mock()
    model.with_structured_output.return_value.invoke.return_value = output
    changes = EvidenceAgent(model)(state)
    assert [claim.claim for claim in changes['claims']] == ['A scoped example.']
    assert changes['claims'][0].priority == 'normal' and changes['claims'][0].relevance == 'context'
    assert not changes['rejected_evidence']
    assert len(changes['evidence']) == 1
    assert any('Excluded 2 off-topic' in message for message in changes['uncertainties'])
    assert model.with_structured_output.return_value.invoke.call_count == 1


def test_export_decodes_reference_title_entities_without_interpreting_html(state):
    report = render(state)
    sources = list(report.sources)
    sources[0] = sources[0].model_copy(update={'title': '&#x53;ources &amp; methods &#110; &lt;script&gt;alert(1)&lt;/script&gt;'})
    report = report.model_copy(update={'sources': sources})
    exported = report_markdown(report)
    text = BeautifulSoup(MarkdownIt().render(exported), 'html.parser')
    assert 'Sources & methods n' in text.get_text(' ')
    assert text.find('script') is None


def test_partial_report_roundtrips_through_api_storage_and_markdown_download(state, tmp_path, sign_in):
    add_gap(state)
    report = render(state)
    settings = Settings(mock_mode=True)
    repo = SQLRunRepository('sqlite:///'+(tmp_path/'partial.db').as_posix())
    class Completed:
        def __init__(self, *_args, **_kwargs): pass
        def run(self, _request): return report
    try:
        with TestClient(create_app(settings, repository=repo)) as client:
            sign_in(client)
            run_id = client.post('/research', json={'topic': SAMPLE_QUERY}).json()['id']
            assert ResearchWorker(repo, settings, service_factory=Completed).run_once()
            fetched = client.get('/research/'+run_id+'/report')
            assert fetched.status_code == 200 and fetched.json()['status'] == 'partial'
            assert ResearchReport.model_validate(fetched.json()).model_dump() == report.model_dump()
            assert client.get('/research/'+run_id).json()['status'] == 'completed'
            exported = client.get('/research/'+run_id+'/report.md')
            assert exported.status_code == 200 and 'Supported answer with limitations' in exported.text
    finally:
        repo.close()
