"""Search node: reuse the existing providers and safe content retriever."""

from collections.abc import Callable

from deepresearch_ai.services.recovery import ResearchPaused
from deepresearch_ai.config import Settings
from deepresearch_ai.services.runtime import search_call, check_deadline, provider_failure_message, retrieval_call
from deepresearch_ai.graph.state import ResearchError, ResearchState, statuses
from deepresearch_ai.services.retrieval import SourceRetriever, create_source_retriever
from deepresearch_ai.services.search import WebSearchProvider, deduplicate_sources
from deepresearch_ai.services.urls import canonical_url
from deepresearch_ai.services.acquisition import source_capacity


def normalize_query(query: str) -> str:
    return " ".join(query.casefold().split())


class SearchAgent:
    def __init__(
        self, settings: Settings, provider: WebSearchProvider,
        retriever_factory: Callable[[], SourceRetriever] | None = None,
    ):
        self.settings = settings
        self.provider = provider
        self.retriever_factory = retriever_factory or (lambda: create_source_retriever(settings))

    def __call__(self, state: ResearchState) -> dict[str, object]:
        searched = list(state["searched_queries"])
        seen = {normalize_query(query) for query in searched}
        errors = list(state["errors"])
        candidates = []
        for question in state["subquestions"][:5]:
            normalized = normalize_query(question.question)
            if normalized in seen:
                continue
            seen.add(normalized)
            searched.append(question.question)
            try:
                candidates.extend(search_call(self.provider, question.question, self.settings.research_results_per_query))
            except ResearchPaused:
                raise
            except Exception as exc:
                errors.append(ResearchError(
                    step="search", kind=type(exc).__name__,
                    message=provider_failure_message(exc,f'Web search for subquestion {question.question_id}'),
                ))
        existing_ids = {source.source_id for source in state["sources"]}
        # Canonical dedup also catches tracking variants of previously retrieved sources.
        all_candidates = deduplicate_sources([*state["sources"], *candidates])
        attempted_urls = list(state["attempted_urls"])
        candidates = [source for source in all_candidates if source.source_id not in existing_ids
                      and canonical_url(source.resolved_url or source.original_url) not in attempted_urls]
        page_limit = source_capacity(state, self.settings)
        remaining = max(0, self.settings.research_max_page_attempts - state["page_attempts"])
        sources = list(state["sources"])
        attempted = state["page_attempts"]
        if candidates and remaining and len(sources) < page_limit:
            try:
                with self.retriever_factory() as retriever:
                    for source in candidates:
                        if len(sources) >= page_limit or attempted >= self.settings.research_max_page_attempts:
                            break
                        url = canonical_url(source.original_url)
                        if url in attempted_urls:
                            continue
                        check_deadline()
                        attempted += 1
                        attempted_urls.append(url)
                        try:
                            fetched = retrieval_call(retriever,source)
                            sources = deduplicate_sources([*sources, fetched])
                            final_url = canonical_url(fetched.resolved_url or fetched.original_url)
                            if final_url not in attempted_urls:
                                attempted_urls.append(final_url)
                            check_deadline()
                        except ResearchPaused:
                            raise
                        except Exception as exc:
                            errors.append(ResearchError(
                                step="search", kind=type(exc).__name__,
                                message=f"Source {source.source_id} was unreachable, restricted, or unusable.",
                            ))
            except ResearchPaused:
                raise
            except Exception as exc:
                errors.append(ResearchError(step="search", kind=type(exc).__name__, message="Source retriever failed."))
        sources = deduplicate_sources(sources)
        uncertainties = list(state["uncertainties"])
        new_errors = len(errors) > len(state["errors"])
        status = "failed" if not sources else "partial" if new_errors else "completed"
        return {"sources": sources, "searched_queries": searched, "page_attempts": attempted,
                "attempted_urls": attempted_urls,
                "errors": errors, "uncertainties": uncertainties,
                "step_status": statuses(state, "search", status)}
