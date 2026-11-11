from unittest.mock import Mock

import pytest

from deepresearch_ai.config import ConfigurationError, Settings
from deepresearch_ai.services import llm


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_adapter_forwards_model_and_provider_settings(monkeypatch, provider):
    factory = Mock()
    monkeypatch.setattr(llm, "init_chat_model", factory)
    settings = Settings(
        llm_provider=provider,
        llm_model="test-model",
        llm_temperature=0.3,
        llm_timeout_seconds=12,
        llm_max_retries=0,
        **{f"{provider}_api_key": "test-only-key"},
    )
    model = llm.create_chat_model(settings)
    assert model is factory.return_value
    factory.assert_called_once_with(
        "test-model", model_provider=provider, api_key="test-only-key",
        timeout=12.0, max_retries=0, temperature=0.3, max_tokens=8192,
    )
    model.invoke.assert_not_called()


def test_none_temperature_is_not_forwarded(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(llm, "init_chat_model", factory)
    llm.create_chat_model(Settings(openai_api_key="test-only-key", llm_temperature=None))
    assert "temperature" not in factory.call_args.kwargs


def test_missing_key_fails_before_provider_initialization(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(llm, "init_chat_model", factory)
    with pytest.raises(ConfigurationError, match="Missing OPENAI_API_KEY"):
        llm.create_chat_model(Settings())
    factory.assert_not_called()


def test_missing_optional_integration_has_actionable_message(monkeypatch):
    monkeypatch.setattr(llm, "init_chat_model", Mock(side_effect=ImportError("not installed")))
    with pytest.raises(ConfigurationError, match=r"pip install -e.*anthropic"):
        llm.create_chat_model(Settings(llm_provider="anthropic", anthropic_api_key="test-only-key"))


def test_adapter_loads_environment_when_settings_are_omitted(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    monkeypatch.setenv("LLM_MODEL", "configured-model")
    factory = Mock()
    monkeypatch.setattr(llm, "init_chat_model", factory)
    llm.create_chat_model()
    assert factory.call_args.args == ("configured-model",)


def test_gemini_configuration_uses_explicit_api_key_backend(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(llm, "init_chat_model", factory)
    llm.create_chat_model(Settings(
        mock_mode=False, llm_provider="google_genai", llm_model="test-gemini-model",
        google_api_key="test-only-key", llm_temperature=None,
    ))
    factory.assert_called_once_with(
        "test-gemini-model", model_provider="google_genai", api_key="test-only-key",
        timeout=30.0, max_retries=2, vertexai=False, max_tokens=8192,
    )


@pytest.mark.parametrize(("retries", "attempts"), [(0, 1), (1, 2), (5, 6)])
def test_gemini_retry_budget_is_translated_to_total_attempts(monkeypatch, retries, attempts):
    factory = Mock()
    monkeypatch.setattr(llm, "init_chat_model", factory)
    llm.create_chat_model(Settings(
        llm_provider="google_genai", google_api_key="test-only-key", llm_max_retries=retries,
    ))
    assert factory.call_args.kwargs["max_retries"] == attempts


def test_real_gemini_missing_key_fails_before_client_creation(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(llm, "init_chat_model", factory)
    with pytest.raises(ConfigurationError, match="Missing GOOGLE_API_KEY"):
        llm.create_chat_model(Settings(mock_mode=False, llm_provider="google_genai"))
    factory.assert_not_called()


def test_gemini_missing_integration_explains_optional_install(monkeypatch):
    monkeypatch.setattr(llm, "init_chat_model", Mock(side_effect=ImportError("not installed")))
    with pytest.raises(ConfigurationError, match=r"pip install -e.*gemini"):
        llm.create_chat_model(Settings(llm_provider="google_genai", google_api_key="test-only-key"))
