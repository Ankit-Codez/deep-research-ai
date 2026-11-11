"""One small factory isolates provider construction from future agents."""

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

from deepresearch_ai.config import ConfigurationError, Settings
from deepresearch_ai.services.mock_llm import MockChatModel


def create_chat_model(settings: Settings | None = None) -> BaseChatModel:
    """Select an offline mock or construct a real provider model without invoking it.

    Use .invoke() / .ainvoke() on the returned standard LangChain interface.
    An unset temperature is omitted for models requiring provider defaults.
    """
    settings = settings if settings is not None else Settings()
    if settings.mock_mode:
        return MockChatModel()
    key = settings.require_api_key()
    kwargs = {
        "api_key": key.get_secret_value(),
        "timeout": settings.llm_timeout_seconds,
        "max_retries": settings.llm_max_retries,
        "max_tokens": settings.llm_max_output_tokens,
    }
    if settings.llm_temperature is not None:
        kwargs["temperature"] = settings.llm_temperature
    if settings.llm_provider == "google_genai":
        # Use the API-key backend even if the shell has Vertex AI settings.
        kwargs["vertexai"] = False
        # Google counts total attempts, unlike OpenAI/Anthropic's extra retries.
        # In google-genai, attempts=0 enables defaults rather than disabling retries.
        kwargs["max_retries"] = settings.llm_max_retries + 1
        if settings.gemini_thinking_level is not None:
            kwargs["thinking_level"] = settings.gemini_thinking_level
    try:
        return init_chat_model(
            settings.llm_model,
            model_provider=settings.llm_provider,
            **kwargs,
        )
    except ImportError as exc:
        extra = {"anthropic": "anthropic", "google_genai": "gemini"}.get(
            settings.llm_provider
        )
        command = (
            f'python -m pip install -e ".[{extra}]"'
            if extra else "python -m pip install -e ."
        )
        raise ConfigurationError(
            f"Provider integration is missing. Install it with: {command}"
        ) from exc
