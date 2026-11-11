"""Replacement downloads and bounded source follow-ups, entirely offline."""
from unittest.mock import MagicMock, Mock

import pytest
from pydantic import ValidationError

from deepresearch_ai.agents.planner import PlannerAgent
from deepresearch_ai.agents.retry import RetryAgent, can_retry_sources
from deepresearch_ai.agents.search import SearchAgent
from deepresearch_ai.config import Settings
from deepresearch_ai.graph.state import initial_state
from deepresearch_ai.models import ResearchRequest, SourceDocument
from deepresearch_ai.models.runs import ResearchDepth
from deepresearch_ai.services.depth import settings_for_depth
from deepresearch_ai.services.mock_data import SAMPLE_QUERY, sample_sources
from deepresearch_ai.services.mock_llm import MockChatModel
from deepresearch_ai.services.retrieval import RetrievalError
from deepresearch_ai.services.workflow import GraphResearchService


def planned_state():
    state = initial_state(ResearchRequest(query=SAMPLE_QUERY))
    state.update(PlannerAgent(MockChatModel())(state))
    return state


def blocked_candidates(count=5):
    return [SourceDocument(original_url=f'https://example.com/blocked-{index}',
                           title='Unavailable page', content='Search snippet is not evidence.')
            for index in range(count)]


def retriever_with(fetch):
    retriever = MagicMock()
    retriever.__enter__.return_value = retriever
    retriever.fetch.side_effect = fetch
    return retriever


def fetch_unless_blocked(source):
    if 'blocked-' in source.original_url:
        raise RetrievalError('Page access restricted')
    return source


def test_five_failed_downloads_do_not_prevent_two_later_successes():
    state = planned_state()
    provider = Mock(search=Mock(return_value=[*blocked_candidates(), *sample_sources()]))
    retriever = retriever_with(fetch_unless_blocked)
    changes = SearchAgent(Settings(mock_mode=True, research_max_pages=2), provider,
                          lambda: retriever)(state)
    assert changes['sources'] == sample_sources()
    assert changes['page_attempts'] == retriever.fetch.call_count == 7
    assert len(changes['errors']) == 5
    assert all('snippet' not in source.content.casefold() for source in changes['sources'])


def test_replacement_downloads_stop_at_attempt_budget():
    provider = Mock(search=Mock(return_value=[*blocked_candidates(), *sample_sources()]))
    retriever = retriever_with(fetch_unless_blocked)
    changes = SearchAgent(Settings(mock_mode=True, research_max_page_attempts=6), provider,
                          lambda: retriever)(planned_state())
    assert changes['sources'] == sample_sources()[:1]
    assert changes['page_attempts'] == retriever.fetch.call_count == 6


@pytest.mark.parametrize('initially_empty', [False, True])
def test_graph_backfills_sources_before_evidence_extraction(initially_empty):
    initial = [] if initially_empty else [*blocked_candidates(4), sample_sources()[0]]
    provider = Mock(search=Mock(side_effect=[initial] * 3 + [sample_sources()[1:], sample_sources()]))
    retriever = retriever_with(fetch_unless_blocked)
    steps = []
    state = GraphResearchService(Settings(mock_mode=True), search=provider,
        retriever_factory=lambda: retriever, on_step=lambda step, iteration: steps.append(step),
    ).run_state(ResearchRequest(query=SAMPLE_QUERY))
    assert steps == ['planner', 'search', 'retry', 'search', 'evidence', 'verification', 'writer']
    assert len(state['sources']) == 2
    assert state['source_retry_count'] == 1 and state['retry_count'] == 0
    assert state['report'].status == 'complete'
    assert set(state['report'].citations.values()) == {source.source_id for source in state['sources']}
    assert not any('No usable source pages' in item for item in state['report'].uncertainties)
    attempted = [call.args[0].original_url for call in retriever.fetch.call_args_list]
    assert len(attempted) == len(set(attempted))


def test_source_followups_do_not_require_a_model_call():
    settings = Settings(mock_mode=True, research_max_llm_calls=1)
    state = planned_state()
    state['step_status']['search'] = 'partial'
    state['sources'] = sample_sources()[:1]
    state['usage'].llm_calls = 1
    model = Mock()
    changes = RetryAgent(model, settings)(state)
    assert len(changes['retry_queries']) == 2
    assert changes['source_retry_count'] == 1
    assert changes['iteration'] == 2
    model.with_structured_output.assert_not_called()


def test_failed_canonical_urls_are_not_downloaded_again_in_later_rounds():
    state = planned_state()
    first = blocked_candidates(1)[0]
    provider = Mock(search=Mock(return_value=[first]))
    retriever = retriever_with(fetch_unless_blocked)
    node = SearchAgent(Settings(mock_mode=True), provider, lambda: retriever)
    state.update(node(state))
    variant = first.model_copy(update={'original_url': first.original_url + '?utm_source=tracking'})
    provider.search.return_value = [variant, *sample_sources()]
    state['subquestions'] = RetryAgent(Mock(), Settings(mock_mode=True))(state)['subquestions']
    state.update(node(state))
    assert retriever.fetch.call_count == 3  # Failed page once, then two usable pages.
    assert len(state['sources']) == 2


def test_redirect_duplicates_count_once_and_final_target_is_not_refetched():
    first, second = sample_sources()
    alias_a = SourceDocument(original_url='https://example.com/alias-a', title='Alias', content='Snippet')
    alias_b = SourceDocument(original_url='https://example.com/alias-b', title='Alias', content='Snippet')
    provider = Mock(search=Mock(return_value=[alias_a, alias_b, first, second]))

    def fetch(source):
        if source in (alias_a, alias_b):
            return SourceDocument(original_url=source.original_url, resolved_url=first.original_url,
                                  title=first.title, content=first.content)
        return source

    retriever = retriever_with(fetch)
    changes = SearchAgent(Settings(mock_mode=True, research_max_pages=2), provider,
                          lambda: retriever)(planned_state())
    assert len(changes['sources']) == 2
    assert retriever.fetch.call_count == 3
    assert first.original_url not in [call.args[0].original_url for call in retriever.fetch.call_args_list]
    assert len({source.resolved_url or source.original_url for source in changes['sources']}) == 2


def test_quick_depth_can_backfill_without_enabling_claim_research():
    settings = settings_for_depth(Settings(mock_mode=True), ResearchDepth.quick)
    assert settings.research_max_retries == 0
    provider = Mock(search=Mock(side_effect=[[]] * 3 + [sample_sources()]))
    state = GraphResearchService(settings, search=provider).run_state(ResearchRequest(query=SAMPLE_QUERY))
    assert state['report'].status == 'complete'
    assert state['usage'].searches == 4
    assert state['source_retry_count'] == 1 and state['retry_count'] == 0


def test_explicit_one_page_cap_is_honored_without_fictional_second_source():
    state = GraphResearchService(Settings(mock_mode=True)).run_state(
        ResearchRequest(query=SAMPLE_QUERY, max_sources=1))
    assert len(state['sources']) == 1 and state['source_retry_count'] == 0
    assert state['report'].status == 'insufficient_evidence'
    assert any('cap is below' in item for item in state['report'].uncertainties)


@pytest.mark.parametrize('limit', ['attempts', 'searches', 'elapsed', 'rounds', 'runtime'])
def test_each_source_retry_limit_terminates_followups(limit):
    settings = Settings(mock_mode=True)
    state = planned_state()
    state['step_status']['search'] = 'failed'
    if limit == 'attempts':
        state['page_attempts'] = settings.research_max_page_attempts
    elif limit == 'searches':
        state['usage'].searches = settings.research_max_searches
    elif limit == 'elapsed':
        state['usage'].elapsed_seconds = settings.research_max_elapsed_seconds
    elif limit == 'rounds':
        state['source_retry_count'] = settings.research_max_source_retries
    else:
        state['usage'].limit_reason = 'Budget exhausted'
    assert not can_retry_sources(state, settings)
    assert RetryAgent(Mock(), settings)(state)['retry_queries'] == []


def test_unavailable_pages_stop_with_explicit_shortage_and_no_snippet_evidence():
    provider = Mock(search=Mock(return_value=blocked_candidates(8)))
    retriever = retriever_with(fetch_unless_blocked)
    state = GraphResearchService(Settings(mock_mode=True, research_max_page_attempts=6),
        search=provider, retriever_factory=lambda: retriever,
    ).run_state(ResearchRequest(query=SAMPLE_QUERY))
    assert state['page_attempts'] == 6 and state['source_retry_count'] == 0
    assert state['sources'] == state['evidence'] == []
    assert state['report'].citations == {}
    assert any('Only 0 usable page(s)' in item and '6/6' in item
               for item in state['report'].uncertainties)


@pytest.mark.parametrize(('field', 'value'), [
    ('research_max_page_attempts', 0), ('research_max_page_attempts', 101),
    ('research_max_source_retries', -1), ('research_max_source_retries', 6),
])
def test_acquisition_limits_are_validated(field, value):
    with pytest.raises(ValidationError):
        Settings(mock_mode=True, **{field: value})
