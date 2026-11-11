"""Application facade for the bounded research graph; Phase 2 remains available unchanged."""

from collections.abc import Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph.state import CompiledStateGraph

from deepresearch_ai.agents.evidence import EvidenceAgent
from deepresearch_ai.agents.planner import PlannerAgent
from deepresearch_ai.agents.search import SearchAgent
from deepresearch_ai.agents.verification import VerificationAgent
from deepresearch_ai.agents.writer import WriterAgent
from deepresearch_ai.agents.retry import RetryAgent, can_retry, can_retry_sources
from deepresearch_ai.services.runtime import ResearchRuntime
from deepresearch_ai.config import Settings
from deepresearch_ai.graph.state import ResearchState, initial_state
from deepresearch_ai.graph.workflow import build_research_graph
from deepresearch_ai.graph.workflow import after_planner, after_search, after_evidence
from deepresearch_ai.models import ResearchReport, ResearchRequest
from deepresearch_ai.services.llm import create_chat_model
from deepresearch_ai.services.retrieval import SourceRetriever
from deepresearch_ai.services.search import WebSearchProvider, create_web_search


class GraphResearchService:
    def __init__(
        self, settings: Settings, *, model: BaseChatModel | None = None,
        search: WebSearchProvider | None = None,
        retriever_factory: Callable[[], SourceRetriever] | None = None,
        on_step: Callable[[str, int], None] | None = None,
        on_update: Callable[[ResearchState], None] | None = None,
    ):
        self.settings = settings
        self.model = model
        self.search = search
        self.retriever_factory = retriever_factory
        self.on_step = on_step
        self.on_update = on_update
        self.persistence = None
        self.entry_node = 'planner'

    def build_graph(self) -> CompiledStateGraph:
        # Credential checks still fail clearly at the application boundary.
        provider = self.search if self.search is not None else create_web_search(self.settings)
        # Outer admission control counts every retry; disable hidden SDK retries.
        bounded_settings = self.settings.model_copy(update={"llm_max_retries": 0,
            "llm_timeout_seconds": min(self.settings.llm_timeout_seconds, self.settings.research_max_elapsed_seconds)})
        model = self.model if self.model is not None else create_chat_model(bounded_settings)
        runtime = ResearchRuntime(self.settings, on_step=self.on_step, persistence=self.persistence)
        return build_research_graph(
            planner=runtime.wrap("planner", PlannerAgent(model)),
            search=runtime.wrap("search", SearchAgent(self.settings, provider, self.retriever_factory)),
            evidence=runtime.wrap("evidence", EvidenceAgent(model)),
            verification=runtime.wrap("verification", VerificationAgent(model)),
            writer=runtime.wrap("writer", WriterAgent(self.settings)),
            retry=runtime.wrap("retry", RetryAgent(model, self.settings)),
            should_retry=lambda state: can_retry(state, self.settings),
            should_retry_sources=lambda state: can_retry_sources(state, self.settings),
            entry_node=self.entry_node,
        )

    def run_state(self, request: ResearchRequest) -> ResearchState:
        state = initial_state(request)
        if self.persistence is not None and (saved := self.persistence.load()) is not None:
            node, phase, state, frozen = saved
            if frozen['mock_mode'] != self.settings.mock_mode:
                from deepresearch_ai.services.recovery import ResearchPaused
                raise ResearchPaused('Resume requires the original mock/real mode in the worker configuration.',code='resume_mode_mismatch')
            self.settings = self.settings.model_copy(update=frozen)
            if phase == 'completed':
                if node == 'writer':
                    return state
                self.entry_node = self._next_node(node,state)
            else:
                self.entry_node = node
        elif self.persistence is not None:
            # Persist the unsent initial state before provider SDK construction.
            self.persistence.checkpoint('started','planner',state)
        graph = self.build_graph()
        config = {"recursion_limit": 12 + 6 * (self.settings.research_max_retries + self.settings.research_max_source_retries)}
        if self.on_update is None:
            return graph.invoke(state, config=config)
        for state in graph.stream(state, config=config, stream_mode="values"):
            self.on_update(state)
        return state

    def _next_node(self, node, state):
        if node == 'planner':
            return after_planner(state)
        if node == 'search':
            return 'retry' if can_retry_sources(state,self.settings) else after_search(state)
        if node == 'evidence':
            return after_evidence(state)
        if node == 'verification':
            return 'retry' if can_retry(state,self.settings) else 'writer'
        return 'search' if state['retry_queries'] and not state['usage'].limit_reason else 'writer'

    def run(self, request: ResearchRequest) -> ResearchReport:
        state = self.run_state(request)
        if state["report"] is None:
            raise RuntimeError("Research graph ended without a report.")
        return state["report"]
