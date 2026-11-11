"""Read-only administrator summaries and bounded, private account archives."""
from datetime import datetime, time, timedelta, timezone
from tempfile import SpooledTemporaryFile
import json
import zipfile

from sqlalchemy import func, select

from deepresearch_ai.storage.tables import ProviderCallRow, ReportRow, RunRow, UsageEntryRow, UserRow
from deepresearch_ai.storage.usage import AdmissionError


def admin_overview(repository, settings):
    now = datetime.now(timezone.utc)
    start = datetime.combine(now.date(),time.min,timezone.utc)
    with repository.sessions() as session:
        counts = dict(session.execute(select(RunRow.status,func.count()).group_by(RunRow.status)).all())
        oldest = session.scalar(select(func.min(RunRow.updated_at)).where(RunRow.status == 'queued'))
        if oldest and oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        age = max(0,(now-oldest).total_seconds()) if oldest else 0
        failed = session.scalar(select(func.count()).select_from(RunRow).where(
            RunRow.status == 'failed',RunRow.updated_at >= now-timedelta(days=1)))
        insufficient = session.scalar(select(func.count()).select_from(ReportRow).join(RunRow)
            .where(ReportRow.evidence_status == 'insufficient_evidence',RunRow.finished_at >= now-timedelta(days=1)))
        unknown = session.scalar(select(func.count()).select_from(ProviderCallRow).join(RunRow)
            .where(RunRow.status == 'failed',ProviderCallRow.kind.in_(['llm','search']),ProviderCallRow.status.in_(['pending','unknown'])))
        spend = session.execute(select(func.sum(UsageEntryRow.estimated_cost),func.sum(UsageEntryRow.cost_charge),
            func.count()).where(UsageEntryRow.period == now.date())).one()
        no_prices = session.scalar(select(func.count()).select_from(UsageEntryRow).where(
            UsageEntryRow.period == now.date(),UsageEntryRow.cost_budget.is_(None)))
        alerts = []
        if age >= settings.admin_queue_alert_seconds:
            alerts.append({'code':'queue_wait','severity':'warning','message':'Queued research has exceeded the configured wait threshold. Check the worker.'})
        if unknown:
            alerts.append({'code':'provider_unknown','severity':'warning','message':f'{unknown} interrupted provider requests have unknown outcomes. Automatic replay is blocked.'})
        if spend[1] is not None and float(spend[1]) >= settings.admin_daily_spend_alert_usd:
            alerts.append({'code':'spending_threshold','severity':'warning','message':'Today\'s estimated/reserved exposure reached the configured spending-alert threshold.'})
        if no_prices:
            alerts.append({'code':'pricing_unavailable','severity':'info','message':'Some runs have no complete pricing configuration. Spending totals are incomplete.'})
        return {'counts':{key:counts.get(key,0) for key in ('queued','running','completed','failed','cancelled')},
            'oldest_queued_seconds':round(age,1),'failed_last_24h':failed,'insufficient_last_24h':insufficient,
            'unknown_provider_calls':unknown,'day_start':start.isoformat(),
            'estimated_cost_usd':float(spend[0]) if spend[0] is not None else None,
            'estimated_reserved_exposure_usd':float(spend[1]) if spend[1] is not None else None,
            'unknown_cost_runs':no_prices,'spending_alert_usd':settings.admin_daily_spend_alert_usd,'alerts':alerts}


def admin_runs(repository, status, limit, offset):
    fields = ('id','status','depth','current_step','progress','created_at','updated_at','error_code','resume_count')
    with repository.sessions() as session:
        condition = RunRow.status == status if status else True
        total = session.scalar(select(func.count()).select_from(RunRow).where(condition))
        rows = session.scalars(select(RunRow).where(condition).order_by(RunRow.created_at.desc(),RunRow.id)
                               .limit(limit).offset(offset))
        return {'items':[{key:getattr(row,key) for key in fields} for row in rows],
                'total':total,'limit':limit,'offset':offset}


def account_archive(repository, user_id, settings, created_from=None, created_to=None):
    """Build a consistent archive in a bounded spool; no tokens/hashes/config."""
    from deepresearch_ai.storage.sql import detail
    from deepresearch_ai.storage.report_store import load_sources
    from deepresearch_ai.services.presentation import report_markdown
    output = SpooledTemporaryFile(max_size=1_000_000,mode='w+b')
    written = 0
    def serialize(value):
        if isinstance(value,datetime):
            return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
        return str(value)
    def add(archive,name,value):
        nonlocal written
        payload = value.encode() if isinstance(value,str) else json.dumps(value,default=serialize,ensure_ascii=False,indent=2).encode()
        written += len(payload)
        if written > settings.account_export_max_bytes:
            raise AdmissionError('export_too_large','This export is too large. Narrow the research date range.',status=413)
        archive.writestr(name,payload)
    try:
        with repository.sessions() as session:
            if repository.engine.dialect.name == 'postgresql':
                session.connection(execution_options={'isolation_level':'REPEATABLE READ'})
            else:
                session.connection().exec_driver_sql('BEGIN')
            user = session.get(UserRow,user_id)
            if user is None or not user.is_active:
                raise AdmissionError('authentication_required','Sign in to export your account.',status=401)
            with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                add(archive,'account.json',{key:getattr(user,key) for key in ('id','email','name','created_at','is_admin')})
                add(archive,'README.txt','DeepResearch AI account export. Research date filters use UTC. Usage includes all retained numeric records, including deleted runs. Passwords, sessions, provider secrets, internal checkpoints and replay receipts are excluded. Retrieved source text is untrusted data.\n')
                statement = select(RunRow).where(RunRow.user_id == user_id).order_by(RunRow.created_at,RunRow.id)
                if created_from:
                    statement = statement.where(RunRow.created_at >= created_from)
                if created_to:
                    statement = statement.where(RunRow.created_at < created_to)
                for row in session.scalars(statement).yield_per(100):
                    run = detail(row,session)
                    # Public research metadata only; omit internal pricing allocation.
                    add(archive,f'research/{row.id}/run.json',run.model_dump(mode='json',exclude={'cost_rates','token_budget','cost_budget_usd'}))
                    add(archive,f'research/{row.id}/sources.json',[item.model_dump(mode='json') for item in load_sources(session,row.id)])
                    if run.report:
                        add(archive,f'research/{row.id}/report.md',report_markdown(run.report))
                fields = ('id','run_id','period','created_at','outcome','settled','token_charge','admitted_tokens',
                          'reported_tokens','llm_calls','searches','cost_charge','estimated_cost')
                # Separate entries keep memory bounded and avoid one huge JSON array.
                for row in session.scalars(select(UsageEntryRow).where(UsageEntryRow.user_id == user_id)).yield_per(100):
                    add(archive,f'usage/{row.id}.json',{key:getattr(row,key) for key in fields})
        if output.tell() > settings.account_export_max_bytes:
            raise AdmissionError('export_too_large','This export is too large. Narrow the research date range.',status=413)
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise
