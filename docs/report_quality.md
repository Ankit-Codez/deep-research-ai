# Research report quality

The research graph, CLI, durable worker and normalized storage remain in place.
This change improves the answer and its presentation without an extra model call.

## Status rules

| Report status | Meaning |
| --- | --- |
| `complete` | Coverage was judged adequate, all selected claims are supported, and the integrity, step and budget checks passed. |
| `partial` | There are checked findings from at least two cited sources and a directly relevant supported finding, but coverage or unresolved claims leave gaps. |
| `insufficient_evidence` | Fewer than two sources supplied checked support, there are only contextual examples, or an integrity, execution or budget check failed. |

Partial answers still require completed planning, extraction and verification,
valid references, complete passage judgments for supported claims, and no run
budget failure. Contradictory passages remain visible; they cannot contribute a
supported verdict. Citations used only to describe disagreements do not count
toward the two-source support requirement.

A run's `completed` status means that processing ended. Its report status
separately describes the evidence. Optional gaps are displayed separately from
core unresolved claims. Neither category is presented as an established fact.

## Scope and writing

The existing extraction call labels drafts `direct`, `context`, or `off_topic`.
Off-topic candidates and notes are excluded before verification. Contextual
examples have normal priority and appear under **Source-specific examples**.
The prompt asks for central gaps, rather than speculative side assertions, and
requires case studies to retain their scope. Older outputs default to `direct`
for compatibility. Relevance and coverage are model assessments and can be wrong;
they do not provide an independent proof of relevance or source credibility.

The Writer composes paragraphs from claim statements that already passed
verification. Numeric references are derived only from passages assessed as
supporting each claim. It does not generate new factual prose, invent connecting
explanations, or make another paid model call. Full exact passages, explanations,
and supporting/contradicting/irrelevant labels remain in the Evidence tab and API
report. A conservative summary may therefore read like a concise sequence of
verified findings rather than a freely written essay.

Multiple cited URLs do not establish independent corroboration. Source
credibility and factual truth still require judgment beyond this workflow.

## Rendering and downloads

Reference titles decode numeric and common HTML entities into plain React text.
They are never inserted as HTML. References link directly to recorded public
HTTP(S) source URLs, with unsafe links omitted and new tabs protected with
`noopener noreferrer`. Markdown downloads decode titles before escaping them;
HTML, external images, arbitrary links and fabricated citation IDs remain blocked.

The public report schema adds the `partial` value. Existing report rows already
store a status string, so no database migration is required. API clients must
accept all three report statuses.

## Apply locally

Restart the backend and worker to load the updated Python schema and report code.
The Vite development server picks up the React changes. If serving the bundled
dashboard through FastAPI, rebuild it with `npm.cmd run build` from `frontend`.
Then start a new investigation. Historical reports and source snapshots are
preserved; they are not automatically reclassified or rewritten.

Tests cover supported partial answers, failed-check safeguards, scoped examples,
off-topic drafts, safe title decoding, real reference URLs, passage labels, and
partial-report persistence, API responses and downloads using mocked providers.
