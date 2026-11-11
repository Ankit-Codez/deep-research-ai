import asyncio
import builtins
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, ValidationError

from deepresearch_ai import cli
from deepresearch_ai.config import ConfigurationError, Settings
from deepresearch_ai.models import (
    ClaimVerification,
    EvidenceItem,
    ResearchPlan,
    ResearchReport,
    ResearchRequest,
    SourceDocument,
    SubQuestion,
)
from deepresearch_ai.services import llm
from deepresearch_ai.services.mock_data import SAMPLE_QUERY
from deepresearch_ai.services.search import create_web_search


@pytest.fixture
def mock_settings(monkeypatch):
    monkeypatch.setenv("MOCK_MODE", "true")
    monkeypatch.setenv("LLM_PROVIDER", "google_genai")
    monkeypatch.setenv("LLM_MODEL", "unused-in-mock-mode")
    return Settings()


def test_mock_mode_never_initializes_or_imports_real_provider_clients(
    monkeypatch, mock_settings, capsys,
):
    factory = Mock(side_effect=AssertionError("Real model initialization is forbidden."))
    monkeypatch.setattr(llm, "init_chat_model", factory)
    key_check = Mock(side_effect=AssertionError("Mock mode must not request credentials."))
    monkeypatch.setattr(Settings, "require_api_key", key_check)
    original_import = builtins.__import__

    def forbid_provider_import(name, *args, **kwargs):
        if name.split(".")[0] in {
            "langchain_openai", "langchain_anthropic", "langchain_google_genai",
            "openai", "anthropic", "tavily",
        } or name.startswith("google.genai"):
            raise AssertionError(f"Mock mode imported a real provider: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbid_provider_import)
    model = llm.create_chat_model(mock_settings)
    assert isinstance(model.invoke(SAMPLE_QUERY), AIMessage)
    assert isinstance(model.with_structured_output(ResearchReport).invoke(SAMPLE_QUERY), ResearchReport)
    assert len(create_web_search(mock_settings).search(SAMPLE_QUERY)) == 2
    assert cli.main(["smoke"]) == 0
    assert "MOCK SAMPLE" in capsys.readouterr().out
    factory.assert_not_called()
    key_check.assert_not_called()
    assert mock_settings.google_api_key is None
    assert mock_settings.gemini_api_key is None
    assert mock_settings.tavily_api_key is None


def test_mock_text_is_deterministic_across_calls_and_instances(mock_settings):
    first = llm.create_chat_model(mock_settings)
    second = llm.create_chat_model(mock_settings)
    response = first.invoke(SAMPLE_QUERY)
    assert response == first.invoke(SAMPLE_QUERY) == second.invoke(SAMPLE_QUERY)
    assert response.response_metadata["mock_mode"] is True
    assert "https://arxiv.org/abs/2005.11401" in response.text


@pytest.mark.parametrize(
    "schema",
    [ResearchRequest, ResearchPlan, SubQuestion, SourceDocument, EvidenceItem,
     ClaimVerification, ResearchReport],
)
def test_mock_structured_responses_use_existing_schemas_and_are_deterministic(mock_settings, schema):
    model = llm.create_chat_model(mock_settings)
    response = model.with_structured_output(schema).invoke(SAMPLE_QUERY)
    repeated = llm.create_chat_model(mock_settings).with_structured_output(schema).invoke(SAMPLE_QUERY)
    assert isinstance(response, schema)
    assert response.model_dump_json() == repeated.model_dump_json()
    assert schema.model_validate_json(response.model_dump_json()) == response


def test_mock_structured_raw_envelope_matches_langchain_contract(mock_settings):
    response = llm.create_chat_model(mock_settings).with_structured_output(
        ResearchReport, include_raw=True,
    ).invoke(SAMPLE_QUERY)
    assert isinstance(response["raw"], AIMessage)
    assert isinstance(response["parsed"], ResearchReport)
    assert response["parsing_error"] is None
    assert ResearchReport.model_validate_json(response["raw"].text) == response["parsed"]


def test_mock_async_interfaces_work_without_credentials(mock_settings):
    async def invoke():
        model = llm.create_chat_model(mock_settings)
        text = await model.ainvoke(SAMPLE_QUERY)
        report = await model.with_structured_output(ResearchReport).ainvoke(SAMPLE_QUERY)
        return text, report

    text, report = asyncio.run(invoke())
    assert text.response_metadata["mock_mode"] is True
    assert isinstance(report, ResearchReport)


def test_mock_search_and_report_share_valid_source_and_evidence_references(mock_settings):
    sources = create_web_search(mock_settings).search(SAMPLE_QUERY)
    report = llm.create_chat_model(mock_settings).with_structured_output(ResearchReport).invoke(SAMPLE_QUERY)
    assert sources == report.sources
    for item in report.evidence:
        source = next(source for source in sources if source.source_id == item.source_id)
        assert item.excerpt in source.content
    assert set(report.citations.values()) == {source.source_id for source in sources}


def test_mock_search_is_deterministic_and_respects_limit(mock_settings):
    search = create_web_search(mock_settings)
    assert search.search(SAMPLE_QUERY) == search.search(SAMPLE_QUERY)
    assert search.search(SAMPLE_QUERY, max_results=1) == search.search(SAMPLE_QUERY)[:1]
    assert len(search.search(SAMPLE_QUERY, max_results=20)) == 2


def test_mock_returns_fresh_data_without_shared_mutation(mock_settings):
    structured = llm.create_chat_model(mock_settings).with_structured_output(ResearchReport)
    report = structured.invoke(SAMPLE_QUERY)
    report.sources.clear()
    report.claims[0].evidence_ids.clear()
    assert len(structured.invoke(SAMPLE_QUERY).sources) == 2
    assert len(structured.invoke(SAMPLE_QUERY).claims[0].evidence_ids) == 2


@pytest.mark.parametrize(("query", "limit"), [(" ", 5), (SAMPLE_QUERY, 0), (SAMPLE_QUERY, 21)])
def test_mock_search_validates_input(mock_settings, query, limit):
    with pytest.raises(ValidationError):
        create_web_search(mock_settings).search(query, max_results=limit)


def test_mock_fixtures_are_explicitly_fixed_topic_samples(mock_settings):
    search = create_web_search(mock_settings)
    assert search.search("An unrelated query") == search.search(SAMPLE_QUERY)
    report = llm.create_chat_model(mock_settings).with_structured_output(ResearchReport).invoke("Other query")
    assert report.query == SAMPLE_QUERY
    assert "Fixed demonstration data" in report.limitations[0]


def test_unsupported_mock_schemas_fail_explicitly(mock_settings):
    class UnknownModel(BaseModel):
        value: str

    model = llm.create_chat_model(mock_settings)
    with pytest.raises(ValueError, match="seven project research models"):
        model.with_structured_output(UnknownModel)
    with pytest.raises(ValueError, match="Pydantic model class"):
        model.with_structured_output({"type": "object"})


def test_mock_environment_is_loaded_when_factories_receive_no_settings(mock_settings):
    assert llm.create_chat_model().invoke(SAMPLE_QUERY).response_metadata["mock_mode"]
    assert len(create_web_search().search(SAMPLE_QUERY)) == 2


def test_real_search_factory_selects_tavily_without_making_requests():
    from deepresearch_ai.services.search import TavilyWebSearch

    assert isinstance(
        create_web_search(Settings(mock_mode=False, tavily_api_key="test-only-key")),
        TavilyWebSearch,
    )
