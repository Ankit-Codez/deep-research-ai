"""Depth profiles cap a run's work while preserving configured global ceilings."""
from deepresearch_ai.models.runs import ResearchDepth
from deepresearch_ai.config import Settings

PROFILES = {
    ResearchDepth.quick: {'research_max_pages': 3, 'research_max_retries': 0,
        'research_max_page_attempts': 6, 'research_max_source_retries': 1,
        'research_max_llm_calls': 6, 'research_max_searches': 4,
        'research_max_elapsed_seconds': 90, 'research_max_tokens': 60000},
    ResearchDepth.standard: {'research_max_pages': 5, 'research_max_retries': 1,
        'research_max_page_attempts': 12, 'research_max_source_retries': 2,
        'research_max_llm_calls': 12, 'research_max_searches': 9,
        'research_max_elapsed_seconds': 180, 'research_max_tokens': 150000},
    ResearchDepth.deep: {},
}


def settings_for_depth(settings: Settings, depth: ResearchDepth) -> Settings:
    return settings.model_copy(update={field: min(getattr(settings, field), cap)
                                       for field, cap in PROFILES[depth].items()})
