import pytest
from pydantic import ValidationError

from deepresearch_ai.config import Settings


@pytest.mark.parametrize(("variable", "value"), [
    ("RESEARCH_MAX_PAGES", "0"), ("RESEARCH_MAX_PAGES", "21"),
    ("RESEARCH_RESULTS_PER_QUERY", "11"), ("RESEARCH_MAX_CONTENT_CHARS", "100"),
    ("RESEARCH_MAX_RESPONSE_BYTES", "100"), ("RESEARCH_REQUEST_TIMEOUT", "0"),
    ("RESEARCH_PAGE_DEADLINE", "0"), ("RESEARCH_REQUEST_DELAY", "-1"),
    ("RESEARCH_MAX_REDIRECTS", "6"),
])
def test_invalid_research_limits_are_rejected(monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValidationError):
        Settings()
