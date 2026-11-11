"""Separate the usable-page target from bounded download attempts."""
from deepresearch_ai.config import Settings
from deepresearch_ai.graph.state import ResearchState

MIN_USABLE_PAGES = 2


def source_capacity(state: ResearchState, settings: Settings) -> int:
    return min(state['request'].max_sources, settings.research_max_pages)


def source_target(state: ResearchState, settings: Settings) -> int:
    # Honor an explicitly smaller source cap, e.g. CLI --max-pages 1.
    return min(MIN_USABLE_PAGES, source_capacity(state, settings))


def source_shortfall(state: ResearchState, settings: Settings) -> bool:
    return len(state['sources']) < source_target(state, settings)
