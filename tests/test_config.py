import pytest
from pydantic import ValidationError

from deepresearch_ai.config import ConfigurationError, Settings


def test_defaults_need_no_credentials_until_model_use():
    settings = Settings(_env_file=None)
    assert settings.llm_provider == "openai"
    assert settings.llm_model == "gpt-4.1-mini"
    assert settings.llm_temperature == 0
    assert settings.mock_mode is False
    with pytest.raises(ConfigurationError, match="Missing OPENAI_API_KEY"):
        settings.require_api_key()


def test_environment_overrides_dotenv(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text(
        "LLM_MODEL=dotenv-model\nLLM_TEMPERATURE=0.2\n"
        "OPENAI_API_KEY=dotenv-test-key\nUNRELATED_SETTING=ignored\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_MODEL", "environment-model")
    monkeypatch.setenv("OPENAI_API_KEY", "environment-test-key")
    settings = Settings()
    assert settings.llm_model == "environment-model"
    assert settings.llm_temperature == 0.2
    assert settings.require_api_key().get_secret_value() == "environment-test-key"


def test_dotenv_can_be_disabled(tmp_path):
    (tmp_path / ".env").write_text("LLM_MODEL=dotenv-model\n", encoding="utf-8")
    assert Settings(_env_file=None).llm_model == "gpt-4.1-mini"


def test_only_selected_provider_key_is_required(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "test-anthropic-model")
    monkeypatch.setenv("ANTHROPIC_API_KEY", " test-only-key ")
    settings = Settings()
    assert settings.require_api_key().get_secret_value() == "test-only-key"
    assert settings.openai_api_key is None


def test_wrong_provider_key_does_not_satisfy_credentials():
    settings = Settings(llm_provider="anthropic", openai_api_key="test-only-key")
    with pytest.raises(ConfigurationError, match="Missing ANTHROPIC_API_KEY"):
        settings.require_api_key()


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_key_is_missing(monkeypatch, value):
    monkeypatch.setenv("OPENAI_API_KEY", value)
    with pytest.raises(ConfigurationError, match="Missing OPENAI_API_KEY"):
        Settings().require_api_key()


def test_credentials_are_masked_in_representations():
    settings = Settings(openai_api_key="test-only-secret")
    assert "test-only-secret" not in repr(settings)
    assert "test-only-secret" not in settings.model_dump_json()


@pytest.mark.parametrize("value", ["", "  "])
def test_blank_temperature_omits_provider_parameter(monkeypatch, value):
    monkeypatch.setenv("LLM_TEMPERATURE", value)
    assert Settings().llm_temperature is None


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("LLM_PROVIDER", "unsupported"),
        ("LLM_MODEL", "   "),
        ("LLM_TEMPERATURE", "-0.1"),
        ("LLM_TEMPERATURE", "1.1"),
        ("LLM_TEMPERATURE", "NaN"),
        ("LLM_TIMEOUT_SECONDS", "0"),
        ("LLM_TIMEOUT_SECONDS", "301"),
        ("LLM_MAX_RETRIES", "-1"),
        ("LLM_MAX_RETRIES", "6"),
        ("LOG_LEVEL", "verbose"),
        ("MOCK_MODE", "maybe"),
        ("SEARCH_PROVIDER", "unsupported"),
    ],
)
def test_invalid_environment_values_are_rejected(monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("false", False)])
def test_mock_mode_is_parsed_as_a_boolean(monkeypatch, value, expected):
    monkeypatch.setenv("MOCK_MODE", value)
    assert Settings().mock_mode is expected


def test_mock_mode_environment_overrides_dotenv(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("MOCK_MODE=true\n", encoding="utf-8")
    assert Settings().mock_mode is True
    monkeypatch.setenv("MOCK_MODE", "false")
    assert Settings().mock_mode is False


def test_gemini_requires_key_only_when_real_model_is_requested():
    settings = Settings(llm_provider="google_genai", llm_model="test-gemini-model")
    with pytest.raises(ConfigurationError, match="Missing GOOGLE_API_KEY"):
        settings.require_api_key()


@pytest.mark.parametrize("variable", ["GOOGLE_API_KEY", "GEMINI_API_KEY"])
def test_gemini_accepts_primary_or_fallback_key(monkeypatch, variable):
    monkeypatch.setenv(variable, " test-only-key ")
    settings = Settings(llm_provider="google_genai")
    assert settings.require_api_key().get_secret_value() == "test-only-key"


def test_google_key_has_precedence_over_gemini_fallback():
    settings = Settings(
        llm_provider="google_genai", google_api_key="primary-test-key",
        gemini_api_key="fallback-test-key",
    )
    assert settings.require_api_key().get_secret_value() == "primary-test-key"


def test_future_provider_credentials_are_masked():
    settings = Settings(google_api_key="google-test-secret", tavily_api_key="tavily-test-secret")
    assert "google-test-secret" not in repr(settings)
    assert "tavily-test-secret" not in settings.model_dump_json()
