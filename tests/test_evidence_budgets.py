"""Regressions for provider outages followed by blocked evidence retries."""
from unittest.mock import Mock

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
import pytest

from deepresearch_ai.agents.evidence import EvidenceAgent
from deepresearch_ai.config import Settings
from deepresearch_ai.graph.state import ResearchUsage, initial_state
from deepresearch_ai.models import ResearchRequest, SourceDocument
from deepresearch_ai.models.agents import EvidenceExtraction
from deepresearch_ai.models.runs import ResearchDepth
from deepresearch_ai.services.depth import settings_for_depth
from deepresearch_ai.services.mock_data import SAMPLE_QUERY, sample_sources
from deepresearch_ai.services.mock_llm import MockChatModel
from deepresearch_ai.services.runtime import (
    BudgetExceeded, ResearchRuntime, StepContext, provider_failure_message,
)


class Unavailable(Exception):
    status_code = 503


def test_complete_usage_settles_only_this_attempt_and_its_cost():
    settings = Settings(mock_mode=True, research_input_cost_per_million=1,
                        research_output_cost_per_million=2, research_search_cost_usd=.01)
    context = StepContext(settings, ResearchUsage(), 0, lambda: 0, Mock())
    context.admit('search')
    context.admit('llm', 10000)
    context.record_llm_usage({'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}, 10000)
    assert context.usage.reserved_tokens == context.usage.reported_tokens == 150
    assert context.usage.estimated_cost_usd == pytest.approx(.0102)
    assert context.usage.llm_calls == context.usage.searches == 1


@pytest.mark.parametrize('metadata', [{}, {'total_tokens': 5},
    {'input_tokens': 10, 'output_tokens': 10, 'total_tokens': 5},
    {'input_tokens': 10, 'output_tokens': 10, 'total_tokens': True}])
def test_missing_or_invalid_usage_never_releases_reservations(metadata):
    context = StepContext(Settings(mock_mode=True), ResearchUsage(), 0, lambda: 0, Mock())
    context.admit('llm', 10000)
    before = context.usage.model_copy()
    context.record_llm_usage(metadata, 10000)
    assert context.usage == before


def test_blocked_retry_preserves_both_budget_and_provider_diagnoses():
    settings = Settings(mock_mode=True, research_max_tokens=10000, provider_backoff_seconds=0)
    context = StepContext(settings, ResearchUsage(), 0, lambda: 0, Mock())
    invoke = Mock(side_effect=Unavailable('private-sdk-text'))
    with pytest.raises(BudgetExceeded) as caught:
        context.call('llm', invoke, 1000)
    message = provider_failure_message(caught.value, 'Structured evidence extraction')
    assert 'token-reservation limit' in message and 'temporarily unavailable' in message
    assert 'private-sdk-text' not in message
    assert invoke.call_count == 1


def test_quick_depth_can_retry_long_pages_after_503_then_verify(monkeypatch):
    original = MockChatModel.with_structured_output
    failures = 0
    sizes = []

    def response(model, schema, **options):
        runner = original(model, schema, **options)
        def invoke(messages):
            nonlocal failures
            if schema is EvidenceExtraction:
                sizes.append(len(messages[-1].content.encode('utf-8')))
                if failures == 0:
                    failures += 1
                    raise Unavailable('private-key-must-not-leak')
            result = runner.invoke(messages)
            result['raw'] = AIMessage(content='', usage_metadata={
                'input_tokens': 500, 'output_tokens': 200, 'total_tokens': 700})
            return result
        return RunnableLambda(invoke)

    monkeypatch.setattr(MockChatModel, 'with_structured_output', response)
    from deepresearch_ai.services.workflow import GraphResearchService
    sources = sample_sources()
    # Full retrieved records are long; evidence prompts must be bounded fairly.
    sources = [source.model_copy(update={'content': ((source.content+' ')*100)[:8000]}) for source in sources]
    # Use normal construction to derive the third source's stable URL ID.
    third = SourceDocument(original_url='https://example.com/third', title='Third source', content=sources[0].content)
    sources.append(third)
    search = Mock(search=Mock(return_value=sources))
    class Retriever:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def fetch(self, source): return source
    settings = settings_for_depth(Settings(mock_mode=True, provider_backoff_seconds=0), ResearchDepth.quick)
    state = GraphResearchService(settings, model=MockChatModel(), search=search,
                                 retriever_factory=Retriever).run_state(ResearchRequest(query=SAMPLE_QUERY))
    assert failures == 1 and len(sizes) == 2
    assert state['step_status']['verification'] == 'completed'
    assert state['report'].status == 'complete'
    assert not state['usage'].limit_reason
    assert state['usage'].llm_calls == 4
    assert state['usage'].reserved_tokens <= settings.research_max_tokens
    assert len(state['sources']) == 3
    assert {source.source_id: source.content for source in state['sources']} == {
        source.source_id: source.content for source in sources}
    assert sizes[0] == sizes[1] < sum(len(source.content) for source in sources)
    assert any('shortened source excerpts' in note for note in state['uncertainties'])


def test_unicode_excerpts_remain_exact_and_bounded_without_ascii_expansion():
    state = initial_state(ResearchRequest(query=SAMPLE_QUERY))
    state['sources'] = [source.model_copy(update={'content': '研究結果と証拠。'*2000}) for source in sample_sources()]
    before = [source.content for source in state['sources']]
    result = ResearchRuntime(settings_for_depth(Settings(mock_mode=True), ResearchDepth.quick)).wrap(
        'evidence', EvidenceAgent(MockChatModel()))(state)
    assert result['usage'].reserved_tokens < 30000
    assert all(item.excerpt in before[index] for index, item in enumerate(result['evidence']))
    assert [source.content for source in state['sources']] == before
