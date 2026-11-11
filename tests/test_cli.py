from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from deepresearch_ai import cli
from deepresearch_ai.services import llm


def test_smoke_invokes_model_and_prints_text(monkeypatch, capsys):
    model = Mock()
    model.invoke.return_value = AIMessage(content="DeepResearch AI is ready.")
    factory = Mock(return_value=model)
    monkeypatch.setattr(cli, "create_chat_model", factory)
    assert cli.main(["smoke", "--prompt", "Say ready."]) == 0
    model.invoke.assert_called_once_with("Say ready.")
    assert capsys.readouterr().out == "DeepResearch AI is ready.\n"


def test_smoke_supports_standard_text_blocks(monkeypatch, capsys):
    model = Mock()
    model.invoke.return_value = AIMessage(content=[{"type": "text", "text": "Ready."}])
    monkeypatch.setattr(cli, "create_chat_model", Mock(return_value=model))
    assert cli.main(["smoke"]) == 0
    assert capsys.readouterr().out == "Ready.\n"


def test_missing_key_returns_configuration_error_without_model_creation(monkeypatch, caplog):
    factory = Mock()
    monkeypatch.setattr(llm, "init_chat_model", factory)
    assert cli.main(["smoke"]) == 2
    assert "Missing OPENAI_API_KEY" in caplog.text
    factory.assert_not_called()


def test_invalid_configuration_returns_clear_error(monkeypatch, caplog):
    monkeypatch.setenv("LLM_TEMPERATURE", "not-a-number")
    factory = Mock()
    monkeypatch.setattr(cli, "create_chat_model", factory)
    assert cli.main(["smoke"]) == 2
    assert "llm_temperature" in caplog.text
    assert "not-a-number" not in caplog.text
    factory.assert_not_called()


def test_provider_failure_is_nonzero_without_leaking_exception(monkeypatch, caplog):
    model = Mock()
    model.invoke.side_effect = RuntimeError("sensitive-test-only-value")
    monkeypatch.setattr(cli, "create_chat_model", Mock(return_value=model))
    assert cli.main(["smoke"]) == 1
    assert "Model invocation failed (RuntimeError)" in caplog.text
    assert "sensitive-test-only-value" not in caplog.text


def test_empty_response_returns_nonzero(monkeypatch, caplog):
    model = Mock()
    model.invoke.return_value = AIMessage(content=" ")
    monkeypatch.setattr(cli, "create_chat_model", Mock(return_value=model))
    assert cli.main(["smoke"]) == 1
    assert "returned no text" in caplog.text


def test_blank_prompt_returns_usage_error(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(cli, "create_chat_model", factory)
    with pytest.raises(SystemExit) as error:
        cli.main(["smoke", "--prompt", " "])
    assert error.value.code == 2
    factory.assert_not_called()
