"""Per-run admission control and transient retries, shared by sequential graph nodes."""

import json
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Callable

from deepresearch_ai.config import Settings

import httpx
from langchain_core.language_models.chat_models import BaseChatModel

from deepresearch_ai.graph.state import ResearchError, ResearchUsage, statuses
from deepresearch_ai.services.recovery import ResearchPaused

logger = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    """A run limit was reached; no further provider calls are admitted."""


class StructuredOutputError(ValueError):
    """Safe diagnostics for unusable structured responses; never store raw output."""

    MESSAGES = {
        "truncated": "The model reached its output-token limit before completing the structured response. "
                     "Increase LLM_MAX_OUTPUT_TOKENS within the run budget or lower GEMINI_THINKING_LEVEL for a compatible Gemini model.",
        "blocked": "The model provider blocked the response. No evidence or citations were accepted from it.",
        "invalid": "The model response did not match the required schema. No incomplete response was accepted.",
    }

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(self.MESSAGES[reason])


@dataclass
class StepContext:
    settings: Settings
    usage: ResearchUsage
    started_at: float
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    persistence: object | None = None

    def check(self):
        self.usage.elapsed_seconds = max(0, self.clock() - self.started_at)
        if self.usage.limit_reason:
            raise BudgetExceeded(self.usage.limit_reason)
        if self.usage.elapsed_seconds >= self.settings.research_max_elapsed_seconds:
            self.stop("Research elapsed-time limit reached.")

    def stop(self, reason):
        self.usage.limit_reason = reason
        raise BudgetExceeded(reason)

    def admit(self, kind, input_size=0):
        self.check()
        if kind == 'fetch':
            if self.usage.page_requests >= self.settings.research_max_page_attempts:
                self.stop('Research page-request limit reached.')
            self.usage.page_requests += 1
            return
        llm = kind == "llm"
        if llm and self.usage.llm_calls >= self.settings.research_max_llm_calls:
            self.stop("Research LLM-call limit reached.")
        if not llm and self.usage.searches >= self.settings.research_max_searches:
            self.stop("Research search-call limit reached.")
        tokens = input_size + self.settings.llm_max_output_tokens if llm else 0
        if self.usage.reserved_tokens + tokens > self.settings.research_max_tokens:
            self.stop("Research token-reservation limit reached.")
        cost = ((input_size * self.settings.research_input_cost_per_million
                 + self.settings.llm_max_output_tokens * self.settings.research_output_cost_per_million) / 1_000_000
                if llm else self.settings.research_search_cost_usd)
        if (self.settings.research_max_cost_usd is not None
                and (self.usage.estimated_cost_usd or 0) + cost > self.settings.research_max_cost_usd):
            self.stop("Research estimated-cost limit reached.")
        self.usage.reserved_tokens += tokens
        if any((self.settings.research_input_cost_per_million,
                self.settings.research_output_cost_per_million, self.settings.research_search_cost_usd)):
            self.usage.estimated_cost_usd = (self.usage.estimated_cost_usd or 0) + cost
        if llm:
            self.usage.llm_calls += 1
        else:
            self.usage.searches += 1

    def call(self, kind, operation, input_size=0, *, identity='', encode=None, decode=None):
        max_attempts = 1 if kind == 'fetch' else self.settings.provider_max_retries + 1
        previous_failure = None
        for attempt in range(max_attempts):
            try:
                if self.persistence is None:
                    self.admit(kind, input_size)
                result = (self.persistence.call(self,kind,operation,input_size,identity,encode,decode)
                          if self.persistence is not None else operation())
                self.check()
                return result
            except Exception as exc:
                if isinstance(exc, BudgetExceeded) and previous_failure is not None:
                    # Preserve the provider failure when admission blocks its retry.
                    raise exc from previous_failure
                if isinstance(exc, (BudgetExceeded,ResearchPaused)) or not transient(exc) or attempt >= max_attempts-1:
                    raise
                previous_failure = exc
                delay = min(8, self.settings.provider_backoff_seconds * (2 ** attempt))
                self.check()
                if self.usage.elapsed_seconds + delay >= self.settings.research_max_elapsed_seconds:
                    self.stop("Research elapsed-time limit reached before provider retry.")
                logger.info(json.dumps({"event": "provider_retry", "provider_type": kind, "attempt": attempt + 1}))
                self.sleep(delay)

    def record_llm_usage(self, metadata, input_size):
        """Settle this attempt only when complete provider token counts exist.

        Unknown/failed attempts keep their conservative reservation. Settlement
        happens before durable receipts, so replay never releases it twice.
        """
        counts = [metadata.get(key) for key in ('input_tokens', 'output_tokens', 'total_tokens')]
        if not all(type(value) is int and value >= 0 for value in counts):
            return
        incoming, outgoing, total = counts
        if total < incoming + outgoing:
            return
        self.usage.reported_tokens += total
        self.usage.usage_reported_calls += 1
        reserved = input_size + self.settings.llm_max_output_tokens
        self.usage.reserved_tokens += total - reserved
        if self.usage.estimated_cost_usd is not None:
            input_rate = self.settings.research_input_cost_per_million
            output_rate = self.settings.research_output_cost_per_million
            before = (input_size * input_rate + self.settings.llm_max_output_tokens * output_rate) / 1_000_000
            after = (incoming * input_rate + max(outgoing, total-incoming) * output_rate) / 1_000_000
            self.usage.estimated_cost_usd = max(0, self.usage.estimated_cost_usd + after-before)
        if self.usage.reserved_tokens > self.settings.research_max_tokens:
            self.usage.limit_reason = 'Research token-reservation limit reached.'
        elif (self.settings.research_max_cost_usd is not None
              and (self.usage.estimated_cost_usd or 0) > self.settings.research_max_cost_usd):
            self.usage.limit_reason = 'Research estimated-cost limit reached.'


def transient(exc: Exception) -> bool:
    """Recognize public SDK status fields, including wrapped Gemini errors."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if status is None:
            status = getattr(getattr(exc, "response", None), "status_code", None)
        if (isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, TimeoutError, ConnectionError))
                or type(exc).__name__ in {"APITimeoutError", "APIConnectionError", "ServiceUnavailable", "DeadlineExceeded"}
                or getattr(exc, "transient", False) is True
                or isinstance(status, int) and status in {408, 429, 500, 502, 503, 504}):
            return True
        exc = exc.__cause__
    return False


def provider_failure_message(exc: Exception, action: str = 'Research planning') -> str:
    message = _provider_failure_message(exc,action)
    return message.replace('LLM','search') if action.casefold().startswith('web search') else message


def _provider_failure_message(exc: Exception, action: str) -> str:
    """Explain provider failures without exposing raw SDK messages or credentials."""
    causes = []
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        causes.append(current)
        current = current.__cause__ or current.__context__
    statuses = {status for cause in causes
                for status in (getattr(cause, 'status_code', None), getattr(cause, 'code', None),
                               getattr(getattr(cause, 'response', None), 'status_code', None))
                if isinstance(status, int)}
    budget = next((cause for cause in causes if isinstance(cause, BudgetExceeded)), None)
    prefix = f'{action} stopped. {budget} ' if budget else f'{action} failed. '
    for cause in causes:
        if isinstance(cause, StructuredOutputError):
            return prefix + StructuredOutputError.MESSAGES[cause.reason]
    if any(getattr(cause, 'winerror', None) == 10013 for cause in causes):
        return prefix + 'Windows blocked the worker network connection. Restart the worker with network access.'
    if statuses & {401, 403}:
        return prefix + 'The LLM provider denied access. Check the worker API key and model permissions.'
    if 400 in statuses:
        return prefix + 'The LLM provider rejected the request format or settings. Check model compatibility and the structured-output schema.'
    if 429 in statuses:
        return prefix + 'The LLM provider rate or quota limit was reached. Check account limits or try later.'
    if statuses & {500, 502, 503, 504}:
        return prefix + 'The configured LLM provider is temporarily unavailable or overloaded. Try later or choose another available model.'
    if any(isinstance(cause, (httpx.TimeoutException, TimeoutError))
           or type(cause).__name__ in {'APITimeoutError', 'DeadlineExceeded'} for cause in causes):
        return prefix + 'The LLM request timed out. Try later or review the worker model and timeout settings.'
    if any(isinstance(cause, (httpx.NetworkError, ConnectionError, PermissionError)) for cause in causes):
        return prefix + 'The worker could not reach the LLM provider. Check network access and connectivity.'
    return prefix.strip()


_active: ContextVar[StepContext | None] = ContextVar("research_step", default=None)


def remaining_seconds():
    if context := _active.get():
        context.check()
        return context.settings.research_max_elapsed_seconds - context.usage.elapsed_seconds
    return float("inf")


def check_deadline():
    if context := _active.get():
        context.check()


def structured_input_size(schema, messages):
    """Conservative local bound, without calling a remote token-count API."""
    payload = {'schema': schema.model_json_schema(),
               'messages': [message.model_dump(mode='json') for message in messages]}
    return len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))


def evidence_input_allowance():
    """Leave room for transient retries and a subsequent verification call."""
    if (context := _active.get()) is None:
        return None
    remaining = context.settings.research_max_tokens - context.usage.reserved_tokens
    output = context.settings.llm_max_output_tokens
    verification_headroom = output + 4096
    attempts = context.settings.provider_max_retries + 1
    return max(0, (remaining-verification_headroom)//attempts-output)


def structured(model, schema, messages):
    context = _active.get()
    # Include schema and message framing in the conservative UTF-8 byte reservation.
    size = structured_input_size(schema, messages)
    raw_envelope = isinstance(model, BaseChatModel)
    runner = model.with_structured_output(schema, include_raw=True) if raw_envelope else model.with_structured_output(schema)

    def invoke():
        response = runner.invoke(messages)
        if not raw_envelope:
            return response
        raw = response.get("raw")
        usage = getattr(raw, "usage_metadata", None) or {}
        if context is not None:
            context.record_llm_usage(usage, size)
        metadata = getattr(raw, "response_metadata", None) or {}
        finish = str(metadata.get("finish_reason") or metadata.get("stop_reason") or "").casefold()
        if finish in {"max_tokens", "length", "max_output_tokens"}:
            reason = "truncated"
        elif (finish in {"safety", "recitation", "blocklist", "prohibited_content", "spii", "refusal"}
              or (getattr(raw, "additional_kwargs", None) or {}).get("refusal")):
            reason = "blocked"
        elif response.get("parsing_error") or response.get("parsed") is None:
            reason = "invalid"
        else:
            reason = "valid"
        # Log only application-owned labels, never raw text or SDK exceptions.
        logger.info(json.dumps({"event": "structured_response", "schema": schema.__name__, "status": reason}))
        if reason != "valid":
            raise StructuredOutputError(reason)
        return response["parsed"]

    identity = json.dumps({'schema':schema.model_json_schema(),
                           'messages':[message.model_dump(mode='json') for message in messages]},sort_keys=True)
    return context.call("llm", invoke, size, identity=identity,
        encode=lambda value:schema.model_validate(value).model_dump_json(), decode=schema.model_validate_json) if context else invoke()


def search_call(provider, query, max_results):
    from pydantic import TypeAdapter
    from deepresearch_ai.models import SourceDocument
    codec = TypeAdapter(list[SourceDocument])
    operation = lambda: provider.search(query, max_results=max_results)
    context = _active.get()
    return context.call("search", operation, identity=json.dumps([query,max_results]),
                        encode=lambda value:codec.dump_json(value).decode(), decode=codec.validate_json) if context else operation()


def retrieval_call(retriever,source):
    from deepresearch_ai.models import SourceDocument
    context = _active.get()
    operation = lambda: retriever.fetch(source)
    return context.call('fetch',operation,identity=source.model_dump_json(),
        encode=lambda value:value.model_dump_json(),decode=SourceDocument.model_validate_json) if context else operation()


class ResearchRuntime:
    def __init__(self, settings, *, clock=time.monotonic, sleep=time.sleep, on_step=None, persistence=None):
        self.settings, self.clock, self.sleep = settings, clock, sleep
        self.on_step = on_step
        self.persistence = persistence

    def wrap(self, name, node):
        def execute(state):
            if self.persistence is not None:
                self.persistence.checkpoint('started',name,state)
            if self.on_step is not None:
                self.on_step(name, state["iteration"])
            usage = state["usage"].model_copy(deep=True)
            started = state["started_at"] if state["started_at"] is not None else self.clock()
            context = StepContext(self.settings, usage, started, self.clock, self.sleep,self.persistence)
            token = _active.set(context)
            logger.info(json.dumps({"event": "step_start", "step": name, "iteration": state["iteration"]}))
            try:
                if name != "writer":
                    context.check()
                result = node({**state, "usage": usage})
            except BudgetExceeded:
                result = {"errors": [*state["errors"], ResearchError(step=name, kind="BudgetExceeded", message=usage.limit_reason)],
                          "step_status": statuses(state, name, "failed")}
            finally:
                _active.reset(token)
            started = context.started_at
            usage.elapsed_seconds = max(0, self.clock() - started)
            # A synchronous operation may finish after its deadline; suppress further calls.
            if usage.elapsed_seconds >= self.settings.research_max_elapsed_seconds:
                usage.limit_reason = "Research elapsed-time limit reached."
            result.update(usage=usage, started_at=started)
            if name == "writer" and (report := result.get("report")) is not None:
                updates = {"usage": usage.model_dump()}
                if usage.limit_reason and usage.limit_reason not in report.uncertainties:
                    updates.update(status="insufficient_evidence",
                                   uncertainties=[*report.uncertainties, usage.limit_reason],
                                   limitations=[*report.limitations, usage.limit_reason],
                                   body_markdown=report.body_markdown + "\n- " + usage.limit_reason)
                result["report"] = report.model_copy(update=updates)
            logger.info(json.dumps({"event": "step_end", "step": name, "iteration": result.get("iteration", state["iteration"]),
                                   "status": result["step_status"].get(name), **usage.model_dump()}))
            if self.persistence is not None:
                self.persistence.checkpoint('completed',name,{**state,**result})
            return result
        return execute
