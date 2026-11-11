"""A deterministic LangChain chat model with no provider SDK or credentials."""

import json
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from deepresearch_ai.services.mock_data import sample_report, sample_response
from deepresearch_ai.models.pipeline import ResearchSummary, SearchQueries, SummaryFinding
from deepresearch_ai.models import ClaimVerification
from deepresearch_ai.models.research import PassageAssessment
from deepresearch_ai.models.agents import EvidenceCandidate, EvidenceExtraction, PlannerOutput, VerificationOutput, RetryQueries


def mock_payload(input_value: object) -> dict:
    if not isinstance(input_value, list) or not input_value or not isinstance(input_value[-1], BaseMessage):
        raise ValueError("Mock pipeline schemas require a JSON payload in the last chat message.")
    if not isinstance(input_value[-1].content, str):
        raise ValueError("Mock pipeline payload must be JSON text.")
    return json.loads(input_value[-1].content)


def pipeline_sample(schema: type, input_value: object) -> BaseModel:
    """Deterministic Phase 2 fixtures derived only from the supplied payload."""
    payload = mock_payload(input_value)
    question = payload["question"]
    if schema is SearchQueries:
        stem = question[:240]
        return SearchQueries(queries=[
            f"{stem} primary sources", f"{stem} methods and evidence", f"{stem} limitations",
        ])
    relevant = any(term in question.lower() for term in ("retrieval", "rag", "ground", "language model"))
    return ResearchSummary(
        sufficient_evidence=relevant,
        findings=[SummaryFinding(source_id=source["source_id"], excerpt=source["content"][:240])
                  for source in payload["sources"] if len(source["content"]) >= 20],
    )


def agent_sample(schema: type, input_value: object) -> BaseModel:
    """Phase 3 offline responses; semantic verification remains deliberately limited."""
    payload = mock_payload(input_value)
    if schema is RetryQueries:
        return RetryQueries(queries=[f"{claim['claim'][:200]} independent evidence round {payload['round']}"
                                     for claim in payload['claims'][:3]])
    if schema is PlannerOutput:
        stem = payload["question"][:240]
        return PlannerOutput(
            strategy="Inspect primary evidence, retrieval methods, and limitations (local mock example).",
            subquestions=[f"{stem} primary evidence", f"{stem} retrieval methods", f"{stem} limitations"],
        )
    if schema is EvidenceExtraction:
        relevant = any(term in payload["question"].lower() for term in ("retrieval", "rag", "ground", "language model"))
        return EvidenceExtraction(
            coverage_sufficient=relevant,
            candidates=[EvidenceCandidate(
                claim=source["content"][:240], source_id=source["source_id"], excerpt=source["content"][:240],
                relevance='direct' if relevant else 'off_topic',
            ) for source in payload["sources"] if len(source["content"]) >= 20],
        )
    evidence = {item["evidence_id"]: item for item in payload["evidence"]}
    results = []
    for claim in payload["claims"]:
        supporting = [reference for reference, item in evidence.items() if claim["claim"] in item["excerpt"]]
        results.append(ClaimVerification(
            claim_id=claim["claim_id"], claim=claim["claim"],
            verdict="supported" if supporting else "insufficient_evidence",
            rationale="The claim matches supplied text verbatim." if supporting else "The local mock cannot establish support for this claim.",
            evidence_ids=list(evidence),
            passage_assessments=[PassageAssessment(evidence_id=reference,
                relation="supports" if reference in supporting else "irrelevant",
                explanation="Literal match in this fixture." if reference in supporting else "No literal support in this fixture.")
                for reference in evidence],
        ))
    return VerificationOutput(results=results)


class MockChatModel(BaseChatModel):
    """Return the same RAG sample for every prompt; never pretend to research it."""

    @property
    def _llm_type(self) -> str:
        return "mock-research"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        report = sample_report()
        references = "\n".join(
            f"[{index}] {source.original_url}"
            for index, source in enumerate(report.sources, start=1)
        )
        text = f"{report.body_markdown}\n\nSample references:\n{references}"
        if stop:
            positions = [text.find(token) for token in stop if token in text]
            if positions:
                text = text[:min(positions)]
        message = AIMessage(
            id="mock-research-response",
            content=text,
            response_metadata={"mock_mode": True},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def with_structured_output(
        self,
        schema: dict[str, Any] | type,
        *,
        include_raw: bool = False,
        **kwargs: Any,
    ) -> RunnableLambda:
        """Support the project's Pydantic schemas, including the standard raw envelope.

        JSON-schema dictionaries, tool calling, and arbitrary schemas are outside
        this small fixture adapter. Unsupported contracts fail explicitly.
        """
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            raise ValueError("Mock structured output requires a project Pydantic model class.")
        pipeline_contract = schema in {SearchQueries, ResearchSummary}
        agent_contract = schema in {PlannerOutput, EvidenceExtraction, VerificationOutput, RetryQueries}
        if not pipeline_contract and not agent_contract:
            sample_response(schema)  # Fail at configuration time for unsupported models.
        if kwargs:
            raise ValueError("Mock structured output does not support additional options.")

        def respond(_input: object) -> BaseModel | dict[str, Any]:
            if agent_contract:
                parsed = agent_sample(schema, _input)
            else:
                parsed = pipeline_sample(schema, _input) if pipeline_contract else sample_response(schema)
            if include_raw:
                return {
                    "raw": AIMessage(
                        id="mock-structured-response",
                        content=parsed.model_dump_json(),
                        response_metadata={"mock_mode": True},
                    ),
                    "parsed": parsed,
                    "parsing_error": None,
                }
            return parsed

        return RunnableLambda(respond)
