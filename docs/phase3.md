# Phase 3: sequential research graph

Historical design notes. Phase 4 adds a bounded research loop and passage-level
verification; see [the current guide](phase4.md) and [current graph](research_graph.md).

The default `research` command now runs five logical agents in LangGraph.
They share one configured chat model; agents are distinct responsibilities,
not separate model subscriptions or autonomous processes. Search and writer
are deterministic application logic. Planner, evidence, and verification each
make at most one logical structured-model invocation per run. Provider retries
still obey `LLM_MAX_RETRIES`.

The original Phase 2 `ResearchService` remains available through
`--workflow single`. Its public Python interface and extraction-only behavior
are preserved. Safe Markdown rendering is shared in `services/reporting.py`.
Both workflows reuse the same search-provider and safe-retriever implementations.

## Run it

From the project root, use mock mode without provider credentials:

```powershell
$env:MOCK_MODE = "true"
.\.venv\Scripts\python.exe -m deepresearch_ai research "How does retrieval-augmented generation ground an LLM response?" --workflow graph --format json
.\.venv\Scripts\python.exe -m deepresearch_ai research "How does retrieval-augmented generation ground an LLM response?" --workflow single --format json
.\.venv\Scripts\python.exe -m pytest -q
```

`--workflow graph` is the default, so existing command lines continue working.
`--max-pages` and `--format markdown` / `json` retain their existing meanings.
For live calls, remove the temporary shell override with
`Remove-Item Env:MOCK_MODE -ErrorAction SilentlyContinue` and configure
`MOCK_MODE=false`, the LLM provider/model/key, and `TAVILY_API_KEY` in `.env`.
Live calls consume the selected providers' quota. Mock runs use fixed, labeled
RAG samples and do not claim to research unrelated questions.

## State and transitions

`graph/state.py` defines `ResearchState`, a `TypedDict` initialized by
`initial_state(request)`. Every collection is fresh for every run.

| State field | Purpose | Updated by |
| --- | --- | --- |
| `question`, `request` | Original question and source budget | Initializer |
| `plan`, `subquestions` | `ResearchPlan` with strategy and 3-5 focused questions | Planner |
| `sources` | Deduplicated, successfully retrieved `SourceDocument` records | Search |
| `evidence`, `claims` | Exact passages with source IDs, and candidate claim/evidence links | Evidence |
| `verification_results` | Structured `ClaimVerification` judgments | Verification |
| `report` | Final validated `ResearchReport` | Writer |
| `errors` | Safe `ResearchError` records with step, kind, and message | Failing nodes |
| `step_status` | Each step's processing status | Every node |
| `iteration` | Current research round, bounded to one | Planner |
| `searched_queries` | Actual attempted query ledger | Search |
| `page_attempts` | Run-wide page-attempt counter | Search |
| `coverage_sufficient`, `rejected_evidence` | Coverage judgment and integrity flags | Evidence |
| `uncertainties` | Coverage gaps, failures, verification limits | Search/writer |

The state contains no API keys, provider clients, or model objects. Dependencies
are injected into callable agent instances; they are not serialized into state.
Nodes return explicit replacement dictionaries and do not mutate incoming
state or nested collections. Since execution is sequential, no append reducers
or concurrent write resolution are needed.

1. **Planner** turns the question into `PlannerOutput`, validates 3-5 unique
   subquestions, assigns deterministic question IDs, and constructs the existing
   `ResearchPlan`. It increments `iteration` from 0 to 1. An invalid/failed plan
   routes directly to writer.
2. **Search** searches each new normalized subquestion once through the existing
   `WebSearchProvider`. It deduplicates canonical URLs, respects the existing
   run-wide page-attempt limit, retrieves content with existing robots/SSRF/
   timeout/size controls, and deduplicates final redirect targets. Missing
   source pages route directly to writer; snippets never become evidence.
3. **Evidence** asks for claim/source/passage candidates. Only known source IDs
   and exact passages from retrieved text are accepted. Duplicate passages are
   merged, and identical claims can reference multiple evidence records.
   Missing usable evidence routes to writer. A partially valid extraction can
   continue with valid records while retaining an integrity warning.
4. **Verification** receives the candidate claims and validated passages in a
   separate model invocation. It labels each claim `supported`, `contradicted`,
   or `insufficient_evidence`. Unknown references, changed claim text, duplicate
   results, and missing results fail closed: affected claims are insufficiently
   supported. A provider failure marks all claims insufficient. It always
   proceeds to writer.
5. **Writer** deterministically renders exact passages referenced by supported
   claims. It owns the citation mapping and never invents source metadata or
   free-form factual prose. Contradicted and insufficient claims remain visible
   as structured results but are excluded from the supported summary. Every
   report includes an explicit Uncertainty section and a structured uncertainty
   list. Early failure paths still produce an insufficient-evidence report.

Processing statuses are `pending`, `completed`, `partial`, `failed`, and
`skipped`. `completed` means that a node produced a valid result; it does not
mean every claim is supported. The writer marks unvisited nodes `skipped` and
finishes its own status. Unsupported judgments, inadequate coverage, rejected
records, or fewer than two cited sources keep the report status
`insufficient_evidence`. Partial search failures can still yield a complete
report if sufficient supported evidence was successfully retrieved.

Verification is an evidence-relative LLM judgment, not independent factual
truth checking. Prompt instructions treat retrieved content and candidate
claims as untrusted data. No tool calls or credentials are provided in agent
prompts. Mechanical source/quote/reference checks are enforced in Python
regardless of what the model claims.

## Graph and bounds

The [Mermaid diagram](research_graph.md) is generated from the actual compiled
graph and also stored as [raw Mermaid](research_graph.mmd). The builder uses:

```python
builder = StateGraph(ResearchState)
builder.add_edge(START, "planner")
builder.add_conditional_edges("planner", after_planner, ...)
builder.add_conditional_edges("search", after_search, ...)
builder.add_conditional_edges("evidence", after_evidence, ...)
builder.add_edge("verification", "writer")
builder.add_edge("writer", END)
graph = builder.compile(name="deepresearch")
```

There are no cycles, parallel fan-out edges, background workers, graph-level
retries, or checkpoints. At most five node executions occur on one path.
`MAX_RESEARCH_ITERATIONS=1` rejects another planner round, and the application
also supplies `recursion_limit=12` as a defensive bound. Search calls are bounded
to 3-5 unique subquestions, and page attempts remain globally capped.

LangGraph makes the shared state and control flow explicit, supports streamed
node updates for debugging, and gives each role an independent testing boundary.
Conditional edges make failure paths inspectable instead of hiding them inside
one long service function. These benefits are useful now without adding
parallelism, persistence, or research loops.

## Inspect a run

```python
from deepresearch_ai.config import Settings
from deepresearch_ai.models import ResearchRequest
from deepresearch_ai.services.workflow import GraphResearchService

service = GraphResearchService(Settings(mock_mode=True))
state = service.run_state(ResearchRequest(query="How does RAG ground an answer?"))
print(state["step_status"])
print(state["iteration"])
print([error.model_dump() for error in state["errors"]])
print(state["report"].model_dump_json(indent=2))

# Or stream explicit node updates:
from deepresearch_ai.graph.state import initial_state
graph = service.build_graph()
for event in graph.stream(initial_state(ResearchRequest(query="How does RAG work?"))):
    print(list(event))

# Regenerate the Mermaid source locally; no external rendering service is needed:
from pathlib import Path
Path("docs/research_graph.mmd").write_text(graph.get_graph().draw_mermaid(), encoding="utf-8")
```

The [example graph report](example_graph_report.json) was produced by a complete
deterministic offline graph run. The [Phase 2 example](example_report.json)
remains available for comparison.

## Validation

Tests exercise each node independently, verify input-state immutability,
check all three verification verdicts and invalid/missing references, and assert
actual compiled-graph execution order and conditional routes. They also cover
query/source deduplication, page budgets, the iteration guard, deterministic
credential-free runs, and both CLI workflows. The complete Phase 1/2 suite
continues running alongside the new tests. Real API clients are mocked in
automated tests, and application network connections are blocked.

Verified full suite: **297 passed in 1.76s**. `pip check` reported **no broken
requirements**. Both the module and installed console entry points produced
identical schema-valid reports within each workflow, without credentials.

No live end-to-end application research run was made for this phase; graph
execution was verified with deterministic local tools and model responses.
