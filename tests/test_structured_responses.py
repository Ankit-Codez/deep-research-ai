"""Never publish truncated/blocked JSON or leak raw model responses."""
import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from pydantic import ValidationError

from deepresearch_ai.agents.planner import PlannerAgent
from deepresearch_ai.config import Settings
from deepresearch_ai.graph.state import initial_state
from deepresearch_ai.models import ClaimVerification, ResearchRequest
from deepresearch_ai.models.agents import PlannerOutput, VerificationOutput
from deepresearch_ai.services import llm
from deepresearch_ai.services.mock_llm import MockChatModel
from deepresearch_ai.services.runtime import ResearchRuntime, StructuredOutputError, structured


def plan():
    return PlannerOutput(strategy='Compare retrieved evidence.', subquestions=['First topic?', 'Second topic?', 'Third topic?'])


def model_response(monkeypatch, *, finish='STOP', parsed=None, error=None, refusal=None):
    response = {'raw': AIMessage(content='private-model-text-and-secret-key',
        response_metadata={'finish_reason': finish}, additional_kwargs={'refusal': refusal} if refusal else {},
        usage_metadata={'input_tokens': 30, 'output_tokens': 20, 'total_tokens': 50}),
        'parsed': parsed, 'parsing_error': error}
    invoke = Mock(return_value=response)
    monkeypatch.setattr(MockChatModel, 'with_structured_output',
                        lambda *args, **kwargs: RunnableLambda(invoke))
    return MockChatModel(), invoke


@pytest.mark.parametrize('finish', ['MAX_TOKENS', 'length', 'max_output_tokens'])
def test_token_limit_rejects_even_a_parseable_partial_response(monkeypatch, finish):
    model, _ = model_response(monkeypatch, finish=finish, parsed=plan())
    with pytest.raises(StructuredOutputError, match='output-token limit'):
        structured(model, PlannerOutput, [])


@pytest.mark.parametrize('finish', ['SAFETY', 'RECITATION', 'BLOCKLIST', 'PROHIBITED_CONTENT', 'SPII'])
def test_blocked_responses_never_become_structured_results(monkeypatch, finish):
    model, _ = model_response(monkeypatch, finish=finish, parsed=plan())
    with pytest.raises(StructuredOutputError, match='blocked'):
        structured(model, PlannerOutput, [])


def test_explicit_refusal_is_rejected_without_echoing_its_text(monkeypatch):
    model, _ = model_response(monkeypatch, parsed=plan(), refusal='sensitive refusal text')
    with pytest.raises(StructuredOutputError) as caught:
        structured(model, PlannerOutput, [])
    assert caught.value.reason == 'blocked'
    assert 'sensitive' not in str(caught.value)


def test_schema_failure_logs_only_safe_labels_and_preserves_actual_usage(monkeypatch, caplog):
    caplog.set_level('INFO', logger='deepresearch_ai.services.runtime')
    model, invoke = model_response(monkeypatch, error=ValueError('secret-key-raw-page-content'))
    state = initial_state(ResearchRequest(query='A research question'))
    changes = ResearchRuntime(Settings(mock_mode=True)).wrap('planner', PlannerAgent(model))(state)
    assert changes['step_status']['planner'] == 'failed'
    assert changes['usage'].llm_calls == invoke.call_count == 1
    assert changes['usage'].reported_tokens == 50
    assert 'required schema' in changes['errors'][0].message
    log_text = ' '.join(record.message for record in caplog.records)
    assert 'secret' not in log_text + changes['errors'][0].model_dump_json()
    events = [json.loads(record.message) for record in caplog.records]
    assert any(event.get('event') == 'structured_response' and event['status'] == 'invalid' for event in events)


def test_truncation_is_actionable_and_does_not_repeat_an_identical_underbudgeted_call(monkeypatch):
    model, invoke = model_response(monkeypatch, finish='MAX_TOKENS', error=ValueError('secret'))
    changes = ResearchRuntime(Settings(mock_mode=True)).wrap('planner', PlannerAgent(model))(
        initial_state(ResearchRequest(query='A research question')))
    assert changes['plan'] is None
    assert 'LLM_MAX_OUTPUT_TOKENS' in changes['errors'][0].message
    assert changes['usage'].reported_tokens == 50
    assert invoke.call_count == 1


def test_complete_schema_response_is_returned(monkeypatch):
    expected = plan()
    model, _ = model_response(monkeypatch, parsed=expected)
    assert structured(model, PlannerOutput, []) == expected


@pytest.mark.parametrize('level', ['minimal', 'low', 'medium', 'high'])
def test_gemini_reasoning_level_is_forwarded_only_to_gemini(monkeypatch, level):
    factory = Mock()
    monkeypatch.setattr(llm, 'init_chat_model', factory)
    settings = Settings(llm_provider='google_genai', google_api_key='test-only-key', gemini_thinking_level=level)
    llm.create_chat_model(settings)
    assert factory.call_args.kwargs['thinking_level'] == level
    llm.create_chat_model(Settings(openai_api_key='test-only-key', gemini_thinking_level=level))
    assert 'thinking_level' not in factory.call_args.kwargs


def test_gemini_reasoning_level_loads_from_environment_and_blank_uses_default(monkeypatch):
    monkeypatch.setenv('GEMINI_THINKING_LEVEL', 'low')
    assert Settings().gemini_thinking_level == 'low'
    monkeypatch.setenv('GEMINI_THINKING_LEVEL', '')
    assert Settings().gemini_thinking_level is None
    monkeypatch.setenv('GEMINI_THINKING_LEVEL', 'unsupported')
    with pytest.raises(ValidationError):
        Settings()


def test_verification_schema_stays_provider_compatible_but_locally_bounded():
    schema = VerificationOutput.model_json_schema()
    assert 'maxItems' not in schema['properties']['results']
    result = ClaimVerification(claim='A factual draft.', verdict='insufficient_evidence',
                               rationale='No evidence available.')
    assert len(VerificationOutput(results=[result] * 120).results) == 120
    with pytest.raises(ValidationError, match='at most 120'):
        VerificationOutput(results=[result] * 121)
