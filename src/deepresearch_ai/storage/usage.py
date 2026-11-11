"""Atomic submission admission and durable, numeric usage accounting.

The user's row is the transaction lock: admissions and account deletion serialize
across processes. SQLite obtains its write lock before reading quota counters.
"""
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
from uuid import uuid4

from sqlalchemy import func, select, update

from deepresearch_ai.models.account import UsageDashboard, UsageTotals
from deepresearch_ai.models.runs import CreateResearch
from deepresearch_ai.services.depth import settings_for_depth
from deepresearch_ai.storage.tables import MetricRow, RunRow, SubmissionKeyRow, UsageEntryRow, UserRow


class AdmissionError(Exception):
    def __init__(self, code, message, *, status=429, retry_after=None):
        self.code, self.message = code, message
        self.status, self.retry_after = status, retry_after
        super().__init__(message)


def lock_user(session, user_id):
    # A no-op UPDATE is a row lock on PostgreSQL and an early write lock on SQLite.
    found = session.execute(update(UserRow).where(UserRow.id == user_id, UserRow.is_active.is_(True))
                            .values(is_active=True).returning(UserRow.id)).scalar_one_or_none()
    if found is None:
        raise AdmissionError('authentication_required', 'Sign in to access your research.', status=401)


def fingerprint(request):
    payload = json.dumps(request.model_dump(mode='json'), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()


def cost_budget(settings):
    if settings.research_max_cost_usd is not None:
        return Decimal(str(settings.research_max_cost_usd))
    rates = [settings.research_input_cost_per_million, settings.research_output_cost_per_million,
             settings.research_search_cost_usd]
    if not all(rate > 0 for rate in rates):
        return None
    return (Decimal(settings.research_max_tokens) * Decimal(str(max(rates[:2]))) / Decimal(1_000_000)
            + Decimal(settings.research_max_searches) * Decimal(str(rates[2])))


def entry_for(session, run_id):
    return session.scalar(select(UsageEntryRow).where(UsageEntryRow.run_id == run_id))


def make_entry(run, settings):
    limits = settings_for_depth(settings, run.depth)
    budget = cost_budget(limits)
    created = run.created_at if run.created_at.tzinfo else run.created_at.replace(tzinfo=timezone.utc)
    return UsageEntryRow(id=str(uuid4()), user_id=run.user_id, run_id=run.id,
        period=created.astimezone(timezone.utc).date(), created_at=created, outcome=run.status, settled=False,
        token_budget=limits.research_max_tokens, token_charge=limits.research_max_tokens,
        cost_budget=budget, cost_charge=budget,
        input_rate=Decimal(str(limits.research_input_cost_per_million)),
        output_rate=Decimal(str(limits.research_output_cost_per_million)),
        search_rate=Decimal(str(limits.research_search_cost_usd)))


def ensure_historical_entries(session, user_id, settings):
    """Make assigned pre-upgrade history visible without inventing missing usage."""
    missing = session.scalars(select(RunRow).where(RunRow.user_id == user_id,
        ~select(UsageEntryRow.id).where(UsageEntryRow.run_id == RunRow.id).exists()))
    for run in missing:
        run.request_hash = fingerprint(CreateResearch(topic=run.topic, instructions=run.instructions, depth=run.depth))
        entry = make_entry(run, settings)
        session.add(entry)
        metrics = list(session.scalars(select(MetricRow).where(MetricRow.run_id == run.id)))
        parsers = {'int': int, 'float': float, 'str': str, 'null': lambda _: None}
        measurements = {item.name: parsers[item.value_kind](item.value) for item in metrics}
        record_measurements(entry, measurements)
        historical_cost = measurements.get('estimated_cost_usd')
        if isinstance(historical_cost, (int,float)) and math.isfinite(historical_cost) and historical_cost >= 0:
            entry.estimated_cost = Decimal(str(historical_cost))
        if run.status not in {'queued', 'running'}:
            entry.settled = True
            entry.token_charge = max(entry.admitted_tokens or 0, entry.reported_tokens or 0)
            entry.cost_charge = entry.estimated_cost
    session.flush()


def totals(entries):
    reported = [row.reported_tokens for row in entries if row.reported_tokens is not None]
    costs = [row.estimated_cost for row in entries if row.estimated_cost is not None]
    settled_costs = [row.cost_charge for row in entries if row.settled and row.cost_charge is not None]
    held_costs = [row.cost_charge for row in entries if not row.settled and row.cost_charge is not None]
    return UsageTotals(submissions=len(entries), reported_tokens=sum(reported) if reported else None,
        admitted_tokens=sum(row.admitted_tokens or 0 for row in entries),
        llm_calls=sum(row.llm_calls or 0 for row in entries), searches=sum(row.searches or 0 for row in entries),
        estimated_cost_usd=float(sum(costs)) if costs else None,
        runs_without_reported_usage=len(entries)-len(reported), runs_without_cost=len(entries)-len(costs),
        charged_tokens=sum(row.token_charge for row in entries if row.settled),
        reserved_tokens=sum(row.token_charge for row in entries if not row.settled),
        charged_cost_usd=float(sum(settled_costs)) if settled_costs else None,
        reserved_cost_usd=float(sum(held_costs)) if held_costs else None)


def dashboard(session, user_id, settings, now):
    entries = list(session.scalars(select(UsageEntryRow).where(UsageEntryRow.user_id == user_id)))
    today_entries = [row for row in entries if row.period == now.date()]
    today = totals(today_entries)
    active = session.scalar(select(func.count()).select_from(RunRow).where(
        RunRow.user_id == user_id, RunRow.status.in_(['queued', 'running'])))
    start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    cost_used = sum((row.cost_charge or Decimal(0) for row in today_entries), Decimal(0))
    remaining_cost = (max(0, float(Decimal(str(settings.quota_daily_cost_usd)) - cost_used))
                      if settings.quota_daily_cost_usd is not None else None)
    return UsageDashboard(period_start=start, resets_at=start+timedelta(days=1), active_runs=active,
        limits={'daily_runs':settings.quota_daily_runs, 'daily_tokens':settings.quota_daily_tokens,
                'daily_cost_usd':settings.quota_daily_cost_usd, 'max_active_runs':settings.quota_max_active_runs},
        remaining={'runs':max(0, settings.quota_daily_runs-today.submissions),
                   'tokens':max(0, settings.quota_daily_tokens-today.charged_tokens-today.reserved_tokens),
                   'cost_usd':remaining_cost, 'active_slots':max(0, settings.quota_max_active_runs-active)},
        today=today, lifetime=totals(entries))


def admit(session, request, user_id, settings, idempotency_key, now):
    """Return (run row, reused). All checks and inserts share one user lock."""
    lock_user(session, user_id)
    ensure_historical_entries(session, user_id, settings)
    request_hash = fingerprint(request)
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
    key = session.get(SubmissionKeyRow, (user_id, key_hash)) if key_hash else None
    if key:
        if key.request_hash != request_hash:
            raise AdmissionError('idempotency_conflict', 'This submission key was already used for different research.', status=409)
        if key.run_id is None:
            raise AdmissionError('submission_deleted', 'The original research was deleted. Start a new submission.', status=409)
        return session.get(RunRow, key.run_id), True
    existing = session.scalar(select(RunRow).where(RunRow.user_id == user_id,
        RunRow.request_hash == request_hash, RunRow.status.in_(['queued', 'running'])).order_by(RunRow.created_at).limit(1))
    if existing:
        if key_hash:
            session.add(SubmissionKeyRow(user_id=user_id, key_hash=key_hash, request_hash=request_hash,
                                         run_id=existing.id, created_at=now))
        return existing, True
    usage = dashboard(session, user_id, settings, now)
    seconds = max(1, math.ceil((usage.resets_at-now).total_seconds()))
    run = RunRow(id=str(uuid4()), user_id=user_id, topic=request.topic, instructions=request.instructions,
        depth=request.depth.value, status='queued', created_at=now, updated_at=now,
        progress=0, iteration=0, request_hash=request_hash)
    entry = make_entry(run, settings)
    reserved_cost = session.scalar(select(func.sum(UsageEntryRow.cost_charge)).where(
        UsageEntryRow.user_id == user_id, UsageEntryRow.period == now.date())) or Decimal(0)
    cost_left = Decimal(str(settings.quota_daily_cost_usd))-reserved_cost if settings.quota_daily_cost_usd is not None else None
    checks = [
        (usage.remaining.active_slots < 1, 'active_run_limit', 'Your active research limit is reached. Wait for or cancel a run.', 5),
        (usage.remaining.runs < 1, 'daily_run_limit', 'Your daily research allowance is exhausted.', seconds),
        (entry.token_budget > usage.remaining.tokens, 'daily_token_limit', 'Not enough daily token allowance for this depth. Choose a lower depth or wait for reset.', seconds),
        (cost_left is not None and entry.cost_budget is not None and entry.cost_budget > cost_left,
         'daily_cost_limit', 'Not enough daily estimated-cost allowance for this research.', seconds),
    ]
    for exceeded, code, message, retry in checks:
        if exceeded:
            raise AdmissionError(code, message, retry_after=retry)
    session.add(run)
    session.flush()
    session.add(entry)
    if key_hash:
        session.add(SubmissionKeyRow(user_id=user_id, key_hash=key_hash, request_hash=request_hash,
                                     run_id=run.id, created_at=now))
    session.flush()
    return run, False


def record_measurements(entry, usage):
    if entry is None or not usage:
        return
    for name, target in [('reserved_tokens','admitted_tokens'), ('llm_calls','llm_calls'), ('searches','searches')]:
        value = usage.get(name)
        if isinstance(value, int) and value >= 0:
            setattr(entry, target, max(getattr(entry, target) or 0, value))
    value = usage.get('reported_tokens')
    if isinstance(value, int) and (usage.get('usage_reported_calls',0) > 0 or value > 0):
        entry.reported_tokens = max(entry.reported_tokens or 0, value)
    value = usage.get('estimated_cost_usd')
    if entry.cost_budget is not None and isinstance(value, (int,float)) and math.isfinite(value) and value >= 0:
        entry.estimated_cost = max(entry.estimated_cost or Decimal(0), Decimal(str(value)))


def settle(session, run_id, outcome, *, usage=None, never_started=False):
    entry = entry_for(session, run_id)
    if entry is None or entry.settled:
        return
    record_measurements(entry, usage)
    entry.outcome, entry.settled = outcome, True
    if never_started:
        entry.token_charge = 0
        entry.cost_charge = Decimal(0) if entry.cost_budget is not None else None
    elif outcome == 'completed':
        entry.token_charge = max(entry.admitted_tokens if entry.admitted_tokens is not None else entry.token_budget,
                                 entry.reported_tokens or 0)
        # Missing cost telemetry cannot release a configured cost reservation.
        entry.cost_charge = entry.estimated_cost if entry.estimated_cost is not None else entry.cost_budget
    # Failed/cancelled in-flight work retains its ceiling: usage may be unknown.
