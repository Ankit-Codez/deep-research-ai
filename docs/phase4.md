# Phase 4: evidence verification and controlled research retries

The default graph performs one initial research round, up to **two source
follow-ups**, and up to **two claim follow-ups**. `--workflow single` preserves the Phase 2 pipeline. No UI,
parallel workers, persistence, or API routes were added.

See the [actual generated graph](research_graph.md), its [Mermaid source](research_graph.mmd),
and [complete mock JSON example](example_graph_report.json).

## How verification works

1. **Planner** produces focused initial search queries. **Search** uses the
   existing provider, canonical URL deduplication, robots-aware public-network
   retriever, and run-wide page-attempt limit.
2. **Evidence** drafts factual research notes and claim/passage candidates.
   Unresolved notes remain claims even without a passage. Claims carry `high`
   or `normal` priority. Claim IDs hash normalized claim text; evidence IDs hash
   the exact source ID plus passage. IDs survive reordered results and retries.
   Existing notes and evidence accumulate rather than disappearing between rounds.
3. A quote is admitted only if its source ID exists and the exact contiguous
   text appears in that source's downloaded content. Search snippets are never
   a fallback. Fabricated passages and unknown source IDs are rejected.
4. **Verification** makes a separate structured model call. It assesses every
   retrieved evidence passage against every claim as `supports`, `contradicts`,
   or `irrelevant`, with an explanation. Missing, duplicate, changed, unknown,
   or incomplete claim/passage references fail closed.
   The prompt includes an explicit claim-by-passage checklist: three passages
   and four claims need twelve individual judgments, including irrelevant ones.
   An otherwise valid response that omits judgments or claims gets **one**
   format-repair attempt for only the incomplete claims. Complete results are
   retained. Fabricated/duplicate/changed references are rejected. Repair uses
   the same token, call, cost, time and durable-receipt controls; it cannot reset
   budgets or start another research round. Remaining incomplete judgments stay
   unverified, and genuinely unsupported claims are never repaired into facts.
5. Application code derives the final verdict from the passage judgments:

   | Passage judgments | Final claim verdict |
   | --- | --- |
   | Support, no contradiction | `supported` |
   | Contradiction, no support | `contradicted` |
   | Both support and contradiction | `insufficient_evidence`, `disagreement=true` |
   | No relevant passages or invalid judgments | `insufficient_evidence` |

   `source_ids` records assessed sources. `supporting_source_ids` and
   `contradicting_source_ids` identify each side. The model cannot override the
   application aggregation with an inconsistent overall verdict.
6. **Retry** generates up to three new targeted queries only for unresolved
   **high-priority** claims. Contradicted claims remain labeled contradicted;
   they do not automatically trigger research. Follow-ups require remaining
   research rounds, model calls, search calls, time, tokens/cost, and page budget.
   An empty or repeated query list ends research. Queries and attempted URLs are
   remembered across rounds, including failed retrieval attempts.
7. **Writer** rechecks passage membership, the full assessment matrix and reference
   integrity. Verified claim statements form cited prose without another LLM call;
   exact passages remain in the evidence records. Source-specific examples and
   optional gaps are separate from the main findings. Unresolved claims get
   explicit labels. Disagreements retain both passages
   with citations in the unresolved section. Attached passages judged irrelevant
   or contradictory are flagged. Report validation rejects unknown citations,
   invalid recorded URL syntax, missing source/evidence references, mismatched
   verification source IDs, and quotes absent from recorded source content.

Reports now distinguish `complete`, `partial` (supported findings with coverage
gaps), and `insufficient_evidence` (failed checks or too little checked support).
See [report quality and presentation](report_quality.md) for the classification
rules, relevance filtering, and safe citation-title rendering.

This is **evidence-relative verification**, not a guarantee of factual truth.
The LLM still judges semantic support and may make mistakes. The application does
not independently establish source credibility. It preserves conflicting
retrieved evidence instead of inventing consensus. Tests script semantic outcomes;
the offline mock verifier only understands literal matches.

## Dates and untrusted content

`SourceDocument` retains `retrieved_at` and optional `published_at` / `modified_at`.
The retriever reads explicit HTML date metadata; invalid or absent dates remain
unknown. These are source-provided dates, not independently verified dates.
Markdown CLI sources display them, and JSON retains them alongside original and
resolved URLs.

All page content, prior claims, and evidence travel as JSON data in human messages,
separate from system instructions. Models receive no filesystem access, credentials,
or operational tools. Obvious instruction-attack patterns quarantine an entire page
from evidence extraction and produce a structured error. That conservative detector
can have false positives and cannot recognize every prompt injection. Schema checks,
quote membership checks, safe rendering, and network restrictions remain necessary;
no claim of complete prompt-injection prevention is made.

## Limits and failure handling

Defaults in `.env.example`:

```dotenv
RESEARCH_MAX_RETRIES=2
RESEARCH_MAX_SOURCE_RETRIES=2
RESEARCH_MAX_PAGE_ATTEMPTS=15
RESEARCH_MAX_LLM_CALLS=16
RESEARCH_MAX_SEARCHES=12
RESEARCH_MAX_ELAPSED_SECONDS=180
RESEARCH_MAX_TOKENS=200000
LLM_MAX_OUTPUT_TOKENS=8192
PROVIDER_MAX_RETRIES=1
PROVIDER_BACKOFF_SECONDS=0.5
```

`RESEARCH_MAX_RETRIES=0` disables claim follow-ups after verification.
`RESEARCH_MAX_SOURCE_RETRIES=0` separately disables source-shortage follow-ups
before evidence extraction. The graph targets two distinct downloaded pages,
subject to an explicitly smaller source cap. `RESEARCH_MAX_PAGES` (default 5)
and `--max-pages` cap usable pages; `RESEARCH_MAX_PAGE_ATTEMPTS` (default 15)
caps all download attempts, including failures, across all rounds. Legacy single
mode retains its original attempt-cap semantics.

Iteration 1 is initial research; the default maximum is iteration 5 (two source
follow-ups plus two claim follow-ups). The planner runs once. Search routes to
retry before evidence extraction when pages are missing and budgets permit;
verification routes there for unresolved high-priority claims. Both paths share
one controlled back edge from retry to search. LangGraph's recursion limit is an
additional guard. See the [depth profiles](phase5.md#depth-and-limits).

Every provider attempt, including failed transient attempts, consumes the same
run-wide counters. Timeouts, connection failures, HTTP 408/429 and selected 5xx
failures receive bounded exponential backoff (0.5, 1, 2... seconds, capped at 8).
Authentication, schema, and ordinary permanent errors are not retried. Graph mode
disables hidden SDK retries; `LLM_MAX_RETRIES` retains its original meaning for
smoke/single mode. Injected custom provider/model test doubles must not add their
own hidden retry loops.

The token limit uses conservative **reservations**: serialized schema/message
UTF-8 bytes plus the configured maximum output tokens for each attempted model
call. Completed responses with valid input, output and total usage settle that
attempt to the provider-reported total (including a complete response rejected
by schema validation). Unknown or failed attempts without usage keep their full
reservation. Provider-reported usage is also retained separately. These are
local budget controls, not a provider invoice or a tokenizer-exact prediction.
Evidence prompts use equal per-source prefixes when needed to leave space for
configured transient retries and a verification call. Full retrieved source
records remain unchanged; quotes must still match them exactly. Shortening is
disclosed in the uncertainty section. Verification remains subject to hard
run limits; reserved headroom cannot guarantee enough evidence or availability.
If a budget blocks a retry, the safe error includes both the local limit and
the original provider error (for example Gemini HTTP 503).
Providers receive `LLM_MAX_OUTPUT_TOKENS`; truncated or malformed structured output
fails closed. Increase this cap if a larger claim/passage matrix needs more output.

An optional estimated USD budget requires all three account-specific prices:

```dotenv
# Example budget only; enter your actual provider prices for the three rates.
RESEARCH_MAX_COST_USD=0.10
RESEARCH_INPUT_COST_PER_MILLION=YOUR_INPUT_PRICE
RESEARCH_OUTPUT_COST_PER_MILLION=YOUR_OUTPUT_PRICE
RESEARCH_SEARCH_COST_USD=YOUR_SEARCH_PRICE
```

Price placeholders must be replaced with positive numbers. No provider prices
are hardcoded. `estimated_cost_usd` is null until rates are configured; it is not
a billing statement. Reservations include each search attempt. Subscription,
credit, cache, reasoning, currency, and provider-specific pricing can differ;
use provider account limits for a hard billing cap.

Elapsed time is checked before calls, during HTTP downloads, between steps, and
before backoff. Page deadlines are reduced to remaining run time. This is a
cooperative deadline: a synchronous model call or operating-system DNS lookup may
finish after it. No new provider work is admitted afterward, and a local writer
still returns an insufficient-evidence report. It does not forcibly kill an
in-flight SDK call.

Each graph step emits `step_start` and `step_end` JSON log messages to stderr,
with step, iteration, status, counters, elapsed seconds, and any budget limit.
Transient attempts emit `provider_retry`. Logs omit questions, source contents,
raw provider exceptions, and credentials. JSON report usage contains the same
counters. Mock report content is deterministic; measured elapsed time varies.

## Files

- `models/agents.py`: factual notes, priority, stable claim IDs, follow-up queries.
- `models/research.py`: passage assessments, verdict/source references, dates,
  report validation, and usage output.
- `agents/evidence.py`, `verification.py`, `writer.py`: extraction, independent
  semantic assessment, deterministic aggregation, and citation-safe rendering.
- `agents/retry.py`: unresolved-priority selection and new-query planning.
- `services/runtime.py`: per-step isolated accounting, admission checks, provider
  retries/backoff, and structured logs. Context-local accounting avoids putting
  clients in graph state and keeps separately invoked nodes easy to test.
- `graph/state.py`, `graph/workflow.py`, `services/workflow.py`: explicit state
  updates and conditional bounded routing around existing agents.
- `services/content_safety.py`: conservative instruction-attack markers.
- `tests/test_phase4.py`: adversarial evidence, conflicts, loops, budgets,
  transient timeouts, real HTML extraction with mock HTTP, and prompt injection.

## Run and validate

From the repository root in PowerShell:

```powershell
$env:MOCK_MODE = "true"
.\.venv\Scripts\python.exe -m deepresearch_ai research "How does RAG ground an answer?" --format json
.\.venv\Scripts\python.exe -m deepresearch_ai research "How does RAG ground an answer?" --workflow single
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

Set `MOCK_MODE=false` and retain your configured Gemini/Tavily credentials to use
real providers. Remove the PowerShell override with `Remove-Item Env:MOCK_MODE`
when you want settings to come from `.env`. Existing `.env` was not modified.
The new controls are graph-specific; legacy single mode keeps its original
verification and retry behavior.

Actual validation results:

- Full suite: **348 passed in 2.96s** (the previous suite contained 297 tests).
- `pip check`: **No broken requirements found.**
- Module and installed console entry points: both graph and single workflows
  returned exit code 0, schema-valid complete mock reports, and two cited sources.
  These checks ran outside the repository with provider credentials removed.
- Diagram generated from the actual compiled graph, including its controlled
  retry-to-search edge.

No live paid model or search calls were made during Phase 4 implementation.
