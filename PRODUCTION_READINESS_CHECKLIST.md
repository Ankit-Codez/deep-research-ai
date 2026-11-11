# DeepResearch AI — production-readiness checklist

Reviewed against the repository on **10 November 2026**.

This is an implementation backlog, not a claim that the application is ready for
public deployment. Checked items describe existing code; unchecked items require
implementation or deployment evidence. Record a test, configuration review, or
runbook beside each item before marking it complete.

| Priority | When to complete it |
| --- | --- |
| P0 | Before an internet-accessible launch, including a small private pilot |
| P1 | Before open registration or wider public use; account recovery also needs an explicit policy for a private pilot |
| P2 | When measured usage or product requirements justify the work |

Keep the first deployment small: one API service, one research worker, PostgreSQL,
and the built React dashboard behind HTTPS. The current database queue can remain
for a pilot after its operational checks pass. A new broker or Kubernetes is not
a prerequisite for this project.

## Existing foundations

- [x] FastAPI enqueues research; a separate worker executes the LangGraph pipeline.
- [x] Durable run states, worker leases, heartbeats, cancellation and fenced writes.
- [x] PostgreSQL persistence with SQLAlchemy and versioned Alembic migrations.
- [x] Normalized tables for accounts, sessions, runs, plans, sources, retrieval snapshots, evidence, claims, reports and citations.
- [x] Foreign keys and composite references connect evidence/citations to sources in the same run.
- [x] Local email/password login with Argon2id hashes and normalized email identifiers.
- [x] Opaque HttpOnly session cookies, hashed session tokens, expiry, logout revocation and CSRF checks.
- [x] User-scoped research queries and tests rejecting another user's run IDs.
- [x] Database-backed login/registration throttling and consistent API errors.
- [x] Per-run limits for searches, pages, text size, model attempts, elapsed time and tokens; optional estimated-cost admission limits.
- [x] Bounded research/provider retries and explicit insufficient-evidence reports.
- [x] Retrieval protections against private-network URLs/redirects, plus source/citation validation and safe report rendering.
- [x] Credential-free deterministic mock mode and automated backend/frontend tests.
- [x] Read-only import of legacy SQLite history with explicit owner assignment.

Previous validation: **475 Python tests**, **3 PostgreSQL integration tests** run
separately, and **38 React tests** passed; the production frontend build and
isolated local HTTP demo passed. These are historical results from the database
and login implementation, not fresh deployment certification.

## P0 — deploy a reproducible, supervised application

- [ ] Put the project under version control and review the initial tracked files. Exclude `.env`, PostgreSQL data, local credential files, demo reports and test artifacts; scan the commit for secrets.
- [ ] Add a reproducible Python dependency lock alongside the existing frontend `package-lock.json`. Test dependency updates before deployment.
- [ ] Add a deployment definition: a container build plus Compose, or equivalent managed-service/process-manager configuration. Run the application as an unprivileged user.
- [ ] Build React during release creation and serve `frontend/dist`; do not expose the Vite development server as the production frontend.
- [ ] Ship migrations and `alembic.ini` with the backend artifact. Run migrations once as a controlled release step before starting compatible API/worker versions.
- [ ] Supervise API and worker independently: automatic startup, bounded restart backoff, resource limits and central stdout/stderr collection.
- [ ] Separate development, staging and production databases, queues and provider credentials. A mock worker must never claim production jobs.
- [ ] Document deployment, rollback and shutdown commands. Choose a shutdown grace period; test SIGTERM/host restart behavior and ensure interrupted jobs reach a visible terminal state without automatic paid-call replay.

Done when a clean staging deployment can be built, migrated, started, restarted
and rolled back using documented steps. FastAPI's deployment guidance covers
HTTPS, startup, restarts and process/resource management:
[deployment concepts](https://fastapi.tiangolo.com/deployment/concepts/).

## P0 — protect the public HTTP boundary and secrets

- [ ] Terminate HTTPS at a reverse proxy or managed ingress; automate certificate renewal. Keep the API's internal listener and PostgreSQL port off the public network.
- [ ] Set `APP_ENV=production`, `AUTH_COOKIE_SECURE=true` and exact HTTPS `AUTH_ALLOWED_ORIGINS`; test the real browser cookie/CSRF flow through that proxy. The current settings guard checks the secure-cookie flag, not the actual TLS deployment.
- [ ] Validate production origins at startup: reject wildcard, malformed or unintended HTTP origins. Configure trusted hosts and trust forwarded headers only from known proxy addresses.
- [ ] Add and test a suitable Content Security Policy, frame protection, referrer policy and HTTPS-only HSTS policy. Preserve existing `nosniff` and private-response `no-store` headers.
- [ ] Enforce incoming request-body limits, connection/time limits and general API rate limits at the edge. Pydantic field limits alone do not bound the size of a body read before validation.
- [ ] Inject provider/database credentials through the deployment's secret store. Separate runtime credentials from migration/admin credentials, restrict permissions and document rotation/revocation.
- [ ] Verify that logs, error tracking, frontend bundles and build artifacts contain no API keys, database passwords, session cookies or CSRF tokens. Keep request bodies and retrieved passages out of routine logs.
- [ ] Decide whether `/docs`, `/redoc` and the OpenAPI schema should be public, restricted or disabled in production; retain authentication on all private research endpoints.

Session deployment checks should follow the
[OWASP session guidance](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html),
including cookie attributes and expiry. Current local HTTP defaults are for development.

## P0 — prevent unbounded research spending and queue growth

- [x] Add durable per-user research submission limits and daily token/cost allowances, enforced atomically when accepting work. Daily estimated-cost limits are optional and require configured pricing.
- [ ] Add a global queue cap; per-user active/queued caps and consistent quota errors with retry guidance are implemented.
- [ ] Refine interrupted-job settlement when final usage can be proven. Shared admission reservations, normal completion settlement and queued-cancellation releases are implemented; running failures/cancellations conservatively retain their ceilings.
- [ ] Set provider-account spending alerts/limits where available. Configure current model/search cost estimates if enabling `RESEARCH_MAX_COST_USD`; estimated application budgets are not billing guarantees.
- [ ] Bound active worker concurrency and test fair service when one account submits many runs.
- [x] Add idempotency for research creation so a double click or network retry cannot enqueue duplicate paid work. Scope keys to the user and validate the request payload matches.
- [ ] Add jitter to transient retry backoff and respect provider retry guidance where available. Do not retry authentication failures or exhausted daily quotas as ordinary transient errors.
- [ ] Add an operator switch to pause new admissions/claims during provider outages or incidents. Never replay failed jobs automatically unless duplicate-call and billing consequences are handled explicitly.

Done when concurrent requests cannot bypass quotas, queue saturation fails
clearly, and an unavailable provider cannot create a retry/spending storm.
Relevant code: `services/runtime.py`, `services/depth.py`, `api/app.py`, `worker.py`.

## P0 — operate PostgreSQL and preserve normalized data

- [ ] Provision a production PostgreSQL service with restricted network access, persistent storage and encrypted connections where traffic crosses hosts. The project-local cluster helper remains a development tool.
- [ ] Separate the runtime role from the schema owner/migration role. Grant runtime access only to the tables and operations it needs; remove schema-changing privileges.
- [ ] Schedule encrypted backups outside the application host with retention and failure alerts. Define acceptable data loss (RPO) and recovery time (RTO).
- [ ] Restore a backup into a separate environment and verify accounts, source snapshots, reports and relationships. Record the measured recovery time; add point-in-time recovery if the chosen RPO requires it.
- [ ] Configure database connection/pool limits, connection timeout, statement timeout and lock timeout against the total API/worker process count.
- [ ] Test migrations from a populated previous release, not only an empty database. Review compatibility and use additive changes when old/new processes may overlap.
- [ ] Review indexes and query plans with realistic history sizes, including ownership filters, queue claims and normalized report loading. Address excessive per-row queries before increasing scale.
- [ ] Define retention/deletion transactions across normalized child and association tables. Delete a shared source snapshot or identity only when no retained run references it.
- [ ] Resolve imported unowned runs through the explicit admin command or archive policy. Never expose them to arbitrary newly registered accounts.

Backup mechanisms and their tradeoffs are described in the
[PostgreSQL 15 backup and restore documentation](https://www.postgresql.org/docs/15/backup.html).
Existing schema/import details are in [the database guide](docs/auth_postgres.md).

## P0 — observe failures and research quality

- [ ] Unify API, worker and graph logs into structured events with request ID, run ID, step, attempt, duration and safe outcome. Existing graph step events do not provide complete cross-process correlation.
- [ ] Record metrics for API latency/errors, queue depth/oldest-job age, worker heartbeat, lease expiry, completion/failure rates, database pool pressure and provider 429/timeout rates.
- [ ] Track research quality separately from job completion: usable sources, extraction failures, supported/contradicted claims, citation validation failures and insufficient-evidence rate.
- [ ] Track reserved/reported tokens and estimated cost per run/user without logging private prompt content. Distinguish unavailable usage data from zero usage.
- [ ] Separate liveness from readiness. Database connectivity exists at `/health`; add worker freshness/queue alerts so an alive API with no functioning worker is detected.
- [ ] Connect safe error reporting and alerts to an operator; test an alert and document response steps for database, provider, worker and suspected credential incidents.

Done when an operator can trace a failed run across services, identify the
failed step and distinguish provider failure from lack of supporting evidence.

## P0 — validate retrieval, evidence and deployment behavior

- [ ] Add outbound network policy around the worker to block internal/metadata destinations as a second layer alongside application SSRF checks. Test DNS changes, redirects, IPv6 and the actual deployment network/proxy setup.
- [ ] Preserve robots/access restrictions, fetch delays and page/byte/time limits. Test slow streams, oversized/decompressed responses and blocked pages without bypassing restrictions to meet the two-source target.
- [ ] Expand prompt-injection regression cases in page titles, snippets and source text. Verify retrieved instructions cannot change tool permissions, reveal secrets or create unsupported references.
- [ ] Add a small versioned research evaluation set with answerable, unanswerable and conflicting-source cases. Record expected evidence behavior, model/prompt versions and quality/cost changes before upgrades.
- [ ] Add CI for Python tests, PostgreSQL integration, frontend tests and production build. Make isolated PostgreSQL tests mandatory in CI rather than silently accepting their default skip.
- [ ] Add lint/type checks, dependency vulnerability review and secret scanning to CI, with a documented process for triaging findings and updating locks.
- [ ] Add browser end-to-end tests through HTTPS for registration/login, reload/session restore, research progress, citations, download, cancellation, logout, expiry and two-user isolation. Component tests do not establish deployed browser behavior.
- [ ] Run staging load/failure tests: simultaneous submissions, slow providers, provider quota exhaustion, worker death, database interruption and proxy restart. Confirm bounded resource use and no duplicate claims or cross-user reads.

Prompt controls are one layer; preserve tool and network boundaries. See
[OWASP prompt-injection prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html).

## P1 — complete public account and privacy workflows

- [ ] Verify email ownership before enabling unrestricted public research; throttle resend attempts and use expiring single-use verification tokens.
- [ ] Add password recovery/change with expiring single-use tokens, generic responses, attempt limits and session revocation. Never log or email plaintext passwords.
- [ ] Offer a session list and revoke-other-sessions action; add recent reauthentication for sensitive account changes.
- [ ] Review login defenses for distributed attempts, breached-password screening and abuse monitoring. Avoid permanent account lockouts that enable denial of service.
- [ ] Add MFA or managed identity when the audience/data warrants it; keep the current session design unless there is a concrete reason to replace it.
- [ ] Add retention automation. Account ZIP export, password-confirmed account deletion, individual research deletion and per-report Markdown export are implemented.
- [ ] Publish retention, privacy and external-provider disclosure: topics, instructions and retrieved passages can be sent to configured LLM/search providers. Document backup deletion lag and provider retention settings.
- [ ] Add minimal audit events for login, recovery, session revocation, ownership assignment and operator actions. Protect access and define retention; do not store secrets in audit events.
- [ ] Review resource-existence/account-enumeration responses and the registration policy before opening signup. Keep registrations disabled or invite-only for a private pilot.
- [ ] Review source-content storage rights and attribution; retain only the content needed for evidence, with a process for removal requests.
- [ ] Improve accessible loading, errors, empty states and mobile flows; show expired sessions and provider outages with safe, actionable messages.

For a small invite-only pilot, document a secure administrator-assisted recovery
process until self-service recovery is implemented. Do not silently leave users
without an account recovery policy.

## P2 — add complexity only for demonstrated needs

- [ ] Adopt a dedicated job broker if database queue contention, routing, scheduling or throughput requires it; retain durable IDs, ownership, idempotency and cancellation semantics.
- [x] Add durable sequential research checkpoints/resume with explicit provider-side-effect and paid-call replay safeguards. Native LangGraph time travel and parallel recovery remain future options.
- [x] Add authenticated SSE progress with reconnection and polling fallback.
- [ ] Consider PostgreSQL row-level security as defense in depth for additional tenants/clients; keep application ownership filters and test the runtime role.
- [ ] Add keyset pagination, batched report loading or object storage when history size and query/storage metrics justify them.
- [ ] Introduce permission-aware caching only after defining freshness, invalidation and cross-user isolation rules; never cache private responses publicly.
- [ ] Scale API/worker replicas based on measured load, database capacity and shared provider budgets; evaluate high availability against the recovery targets.

## Release evidence to attach before launch

- [ ] Record the release revision, dependency locks and deployed migration head.
- [ ] Attach fresh backend/PostgreSQL/frontend/build/HTTPS-browser test results.
- [ ] Record the successful backup restore, rollback drill and worker-restart drill.
- [ ] Record quota, overload and provider-outage checks, plus working alerts.
- [ ] Review production origins/cookies, role permissions, secret injection and exposed ports.
- [ ] Record the pilot audience, registration/recovery policy, retention policy and operator contact.

Suggested implementation order: **deployment and CI → HTTPS/secrets → research
quotas → backups/restore → monitoring/failure drills → public account workflows**.
Keep unresolved P0 items open until there is evidence they work in staging.

## Features to add

- [ ] Email verification.
- [ ] Forgot-password recovery and password change.
- [ ] Active-session management and sign out from all devices.
- [ ] Optional multi-factor authentication.
- [x] Account deletion and individual research deletion.
- [x] Research history search and filters across all saved runs.
- [x] Personal usage, token and estimated-cost dashboard.
- [x] Research quotas with remaining-allowance indicators.
- [x] Duplicate research submission prevention.
- [x] Resumable research after interruptions, with safeguards against repeated paid calls.
- [x] Live progress streaming.
- [x] Clear provider-outage and quota-exhaustion messages.
- [x] Administrator dashboard for queue status, failures and spending alerts.
- [x] Account data export.
- [x] Supported partial-answer status, scoped claim filtering, cited prose, and readable safe reference links. Details: [report quality](docs/report_quality.md).

Implemented feature details, quota defaults and deletion behavior: [account and research controls](docs/account_features.md).

Recovery, live updates, explicit administrator role grants, and private archive details: [operations guide](docs/recovery_operations.md).
