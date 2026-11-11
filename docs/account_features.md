# Research history, deletion, personal usage and quotas

These features extend the existing authenticated FastAPI/React application.
The LangGraph pipeline and standalone CLI remain operational.

## Use the dashboard

- **Overview:** today's remaining runs, available quota tokens and free active-run
  slots appear above the research form. An enabled estimated-cost allowance is
  also shown. Research still uses the selected Quick/Standard/Deep profile.
- **Research history:** search topics and optional instructions across all your
  saved runs. Combine search with status, depth and inclusive UTC creation-date
  filters. Filtering happens in SQL before pagination, not just on the visible page.
- **Delete research:** available in history and in an individual investigation.
  Review the inline confirmation and choose **Permanently delete research**.
  Active work loses write access immediately and stops at its next checkpoint;
  a provider call already in progress may still finish and be billed.
- **Usage & account:** switch between today's and all-time usage. View submissions,
  model/search calls, provider-reported tokens, conservative admitted tokens,
  estimated cost, and quota reservations/charges.
- **Delete account:** enter your current password and type `DELETE`, then confirm.
  The application removes the account, all sessions, owned research and personal
  usage/idempotency records, and returns you to sign-in.

Deleting a run removes its report, evidence, claims, citations, plan and run links.
Unreferenced source snapshots/identities are cleaned up. Source records needed by
another user's research are preserved. Account deletion does not delete unassigned
legacy history or another user's runs. Backup copies, the preserved legacy SQLite
file and historical security throttling buckets have separate retention policies;
the deletion endpoint does not rewrite backups.

## Configurable allowances

The defaults below apply independently to each account:

```dotenv
QUOTA_DAILY_RUNS=20
QUOTA_DAILY_TOKENS=1000000
QUOTA_MAX_ACTIVE_RUNS=3
# Optional, with pricing configuration described below:
# QUOTA_DAILY_COST_USD=1.00
```

Add overrides to the backend `.env` and restart the API and worker. Allowances
reset at midnight **UTC**. Reservations and charges belong to the submission day,
including when a queued job finishes on a later day. Active-run limits span days.
These are application admission limits, not a guarantee about the provider's
billing period, final charges or an account-wide provider spending ceiling.

On submission, one database transaction locks the user's account row, checks
duplicates/quotas, creates the queued run and reserves its depth-specific token
ceiling. PostgreSQL row locks serialize concurrent submissions across processes;
the explicit SQLite test adapter acquires an early write lock.

Quota behavior:

| Event | Accounting |
| --- | --- |
| New accepted run | Counts as one daily submission and reserves the run's token/cost ceiling |
| Duplicate/replayed submission | Returns the existing run without an additional submission or reservation |
| Normal completed report | Settles to recorded admitted token estimates, at least the reported token total; releases unused reservation |
| Queued cancellation/deletion | Releases token/cost reservations; the accepted submission still counts |
| Running cancellation, failure, expiry or deletion | Retains the full ceiling conservatively, because final provider usage may be unknown |
| Research deletion | Retains numeric accounting and replay-protection digests; cannot reset daily allowances |
| Account deletion | Removes that user's accounting and session data from the application database |

Existing assigned history is added to accounting on first usage/admission access.
Recorded historical measurements are preserved; missing historical usage is not
invented. Unassigned imported history remains private until explicitly assigned.

The worker cannot raise a queued run's saved token/cost ceiling if its configuration
changes after submission. Pricing rates used for the estimate are saved with the
reservation. Runtime provider credentials are never stored in the job or exposed
through the usage endpoint.

### Optional estimated-cost limits

No provider prices are hardcoded. To enable a daily cost allowance, configure
`QUOTA_DAILY_COST_USD`, `RESEARCH_MAX_COST_USD`, and positive values for all of:

- `RESEARCH_INPUT_COST_PER_MILLION`
- `RESEARCH_OUTPUT_COST_PER_MILLION`
- `RESEARCH_SEARCH_COST_USD`

Use rates applicable to your configured model/search account. The API rejects
an incomplete daily cost-limit configuration. When all rates are supplied without
an explicit per-run cost limit, admission derives a conservative cost reservation
from the token and search ceilings; the daily cost quota itself still requires
an explicit `RESEARCH_MAX_COST_USD`.

Provider-reported tokens and conservative admitted token estimates are separate.
Some responses do not report token metadata; mock responses do not invent it.
Missing measurements display **Not reported**/**Unavailable**, and the dashboard
shows counts of runs lacking usage/cost data. Known totals can therefore be partial.
An estimated cost is not a provider invoice. Run failures retain conservative
quota charges even when the dashboard has only partial measurements.

## Duplicate submission rules

The React form creates a random idempotency key for a submission and reuses it
after a network failure while the payload stays the same. The server stores a
digest of that key, scoped to the user, alongside a digest of the validated
topic/instructions/depth.

- Repeating a key with the same payload returns the original run, even after it
  completes or the account exhausts its remaining allowance.
- Reusing a key for a different payload returns `409 idempotency_conflict`.
- Repeating a key after its original run was deleted returns
  `409 submission_deleted`; it never recreates paid work.
- Identical validated payloads already queued/running reuse that active run,
  including requests with different keys or no key.
- A completed/failed/cancelled run may be intentionally rerun with a new key.

This is deterministic payload matching, not semantic similarity detection.
Leading/trailing whitespace and blank instructions follow existing schema
normalization; the question's case and wording retain their meaning.

## API additions

All endpoints below require the existing authenticated session. Mutations also
require `X-CSRF-Token`; account deletion requires password reauthentication.

| Endpoint | Behavior |
| --- | --- |
| `GET /account/usage` | Today's and lifetime measurements, limits, remaining allowances and UTC reset time |
| `GET /research?q=...&status=...&depth=...&created_from=YYYY-MM-DD&created_to=YYYY-MM-DD` | User-scoped search/filtering, with existing `limit`/`offset` pagination |
| `POST /research` with optional `Idempotency-Key` | Atomic deduplicated admission; response remains `202` |
| `DELETE /research/{id}` | `204` after deleting an owned run; another user's or absent ID returns `404` |
| `DELETE /auth/me` | Body: `password`, `confirmation: "DELETE"`; `204` after account deletion |

An idempotency key must be 8–128 characters: letters, digits, `.`, `_`, `:` or
`-`, beginning with a letter/digit. The creation response includes
`submission_reused`, and `Idempotency-Replayed: true/false` indicates reuse.
Quota failures return `429` with a safe error code and `Retry-After`:
`active_run_limit`, `daily_run_limit`, `daily_token_limit`, `daily_cost_limit`.
The frontend maps these codes to actionable text without showing raw backend errors.
Vite now proxies `/account` as well as `/auth`, `/research` and `/health`.

## Migration and startup

The additive Alembic revision is `ea6c7d823efb`; it adds normalized
`usage_entries` and `submission_keys` plus an indexed run request digest. Source,
evidence and report storage is preserved. A private database backup was created
under ignored `data/backups/` before applying it on this machine.

For another checkout or database, start PostgreSQL and migrate before starting
the API/worker:

```powershell
.\.venv\Scripts\python.exe scripts/postgres_local.py start
.\.venv\Scripts\python.exe -m deepresearch_ai.db upgrade
```

Continue with the commands in [the startup guide](auth_postgres.md#start-on-this-machine).
Rebuild the frontend when using FastAPI to serve its static build:

```powershell
cd frontend
npm.cmd run build
```

The standalone research CLI is trusted local tooling and does not create account
jobs or consume web-account quotas. Provider-side limits still apply.

## Files and verification

- `storage/usage.py`: atomic admission, historical accounting and quota settlement.
- `storage/deletion.py`: ordered relational deletion and shared-source cleanup.
- `models/account.py`: deletion and usage response contracts.
- `api/app.py`, `api/auth.py`: authenticated endpoints, validation and safe errors.
- `frontend/src/components/AccountView.tsx`: personal usage and account deletion.
- `frontend/src/components/HistoryView.tsx`: server search/filtering and run deletion.
- `frontend/src/components/DeleteResearch.tsx`: explicit research-deletion confirmation.
- `tests/test_account_features.py`: lifecycle, quotas, concurrency, isolation and filtering.
- `tests/test_postgres.py`: actual PostgreSQL migration, locking, deletion and worker checks.
- `frontend/src/test/account.test.tsx`: UI confirmation, allowance, filter, retry-key and error flows.

Run `python -m pytest -q`, `npm.cmd test` and `npm.cmd run build` using the project
environment. PostgreSQL integration remains opt-in with `TEST_POSTGRES_URL` and
uses disposable local schemas. The isolated `scripts/local_demo.py` workflow
exercises actual HTTP with mocked providers; no Gemini/Tavily calls are needed.

Validation: **495 Python tests passed**, with **7 PostgreSQL integration tests**
passing separately, and **45 React tests passed**. TypeScript checking and the
production frontend build passed. Automated providers are mocked.

Safe checkpoint resume, SSE progress, administrator operations and account ZIP
export are now available; see the [operations guide](recovery_operations.md).
Resume reuses the original retained reservation and does not create a new daily
submission. Unknown paid-call outcomes are blocked.
