"""FastAPI application factory. Requests enqueue work; they never invoke the graph."""
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
import logging
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from deepresearch_ai.api.schemas import CreateResearch, ErrorResponse, RunDetail, RunPage, RunStatus
from deepresearch_ai.api.auth import build_auth
from deepresearch_ai.models.auth import UserPublic
from deepresearch_ai.models.account import UsageDashboard
from deepresearch_ai.models.operations import AdminOverview, AdminRunPage
from deepresearch_ai.models.runs import ResearchDepth
from deepresearch_ai.config import Settings
from deepresearch_ai.models import ResearchReport, SourceDocument
from deepresearch_ai.services.presentation import report_markdown
from deepresearch_ai.storage.sql import SQLRunRepository
from deepresearch_ai.storage.usage import AdmissionError

logger = logging.getLogger(__name__)
ERRORS = {code: {'model': ErrorResponse} for code in (401, 403, 404, 409, 413, 422, 429, 500, 503)}


def error_response(status, code, message, details=None):
    return JSONResponse(status_code=status, content={'error': {
        'code': code, 'message': message, 'details': details or []}})


def create_app(settings: Settings | None = None, *, repository: SQLRunRepository | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings()
    owned = repository is None
    repo = repository if repository is not None else SQLRunRepository(settings.database_url.get_secret_value())

    @asynccontextmanager
    async def lifespan(app):
        repo.initialize()
        app.state.repository = repo
        yield
        if owned:
            repo.close()

    app = FastAPI(title='DeepResearch AI', version='0.1.0', lifespan=lifespan)
    auth_router, current_user, mutation_user = build_auth(settings, repo.sessions)
    app.include_router(auth_router)

    @app.exception_handler(AdmissionError)
    async def admission_error(_request, exc):
        response = error_response(exc.status, exc.code, exc.message)
        if exc.retry_after:
            response.headers['Retry-After'] = str(exc.retry_after)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc):
        # Do not echo input values, JSON bodies, or validator exception messages.
        details = [{'field': '.'.join(str(item) for item in error['loc']), 'type': error['type']}
                   for error in exc.errors()]
        return error_response(422, 'validation_error', 'Invalid request. Check the indicated fields.', details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request, exc):
        if isinstance(exc.detail, dict):
            response = error_response(exc.status_code, exc.detail['code'], exc.detail['message'])
            if exc.headers:
                response.headers.update(exc.headers)
            return response
        return error_response(exc.status_code, 'http_error', 'The requested operation is unavailable.')

    @app.exception_handler(SQLAlchemyError)
    async def storage_error(_request, exc):
        logger.error('Storage operation failed (%s).', type(exc).__name__)
        return error_response(503, 'storage_unavailable', 'Research history is temporarily unavailable.')

    @app.exception_handler(Exception)
    async def unexpected_error(_request, exc):
        logger.error('API operation failed (%s).', type(exc).__name__)
        return error_response(500, 'internal_error', 'The request could not be completed.')

    @app.middleware('http')
    async def response_headers(request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        return response

    def get_run(run_id, user_id):
        repo.expire_abandoned()
        run = repo.get_for_user(str(run_id), user_id)
        if run is None:
            raise HTTPException(404, detail={'code': 'run_not_found', 'message': 'Research run not found.'})
        return run

    @app.get('/health')
    def health():
        repo.ping()
        return {'status': 'ok'}

    @app.post('/research', response_model=RunDetail, status_code=202, responses=ERRORS)
    def create_research(payload: CreateResearch, response: Response, user: UserPublic = Depends(mutation_user),
                        idempotency_key: Annotated[str | None, Header(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$')] = None):
        repo.expire_abandoned()
        run = repo.create(payload, user_id=str(user.id), settings=settings, idempotency_key=idempotency_key)
        response.headers['Location'] = f'/research/{run.id}'
        response.headers['Idempotency-Replayed'] = str(run.submission_reused).lower()
        return run

    @app.get('/account/usage', response_model=UsageDashboard, responses=ERRORS)
    def personal_usage(user: UserPublic = Depends(current_user)):
        repo.expire_abandoned()
        return repo.usage_for_user(str(user.id), settings)

    def administrator(user: UserPublic = Depends(current_user)):
        if not user.is_admin:
            raise HTTPException(403,detail={'code':'admin_required','message':'Administrator access is required.'})
        return user

    @app.get('/admin/overview',response_model=AdminOverview,responses=ERRORS)
    def admin_dashboard(_user: UserPublic = Depends(administrator)):
        from deepresearch_ai.storage.operations import admin_overview
        repo.expire_abandoned()
        return admin_overview(repo,settings)

    @app.get('/admin/runs',response_model=AdminRunPage,responses=ERRORS)
    def admin_history(_user: UserPublic = Depends(administrator), status: RunStatus | None = None,
                      limit: Annotated[int,Query(ge=1,le=100)] = 50, offset: Annotated[int,Query(ge=0)] = 0):
        from deepresearch_ai.storage.operations import admin_runs
        return admin_runs(repo,status,limit,offset)

    @app.get('/account/export',responses=ERRORS)
    def export_account(user: UserPublic = Depends(current_user),created_from: date | None = None,created_to: date | None = None):
        from deepresearch_ai.storage.operations import account_archive
        if created_to == date.max or (created_from and created_to and created_from > created_to):
            raise HTTPException(422,detail={'code':'invalid_date_range','message':'Choose a valid date range.'})
        output = account_archive(repo,str(user.id),settings,
            datetime.combine(created_from,time.min,timezone.utc) if created_from else None,
            datetime.combine(created_to+timedelta(days=1),time.min,timezone.utc) if created_to else None)
        def chunks():
            try:
                while data := output.read(65536):
                    yield data
            finally:
                output.close()
        return StreamingResponse(chunks(),media_type='application/zip',background=BackgroundTask(output.close),
            headers={'Content-Disposition':'attachment; filename="deepresearch-account.zip"'})

    @app.post('/research/{run_id}/resume',response_model=RunDetail,status_code=202,responses=ERRORS)
    def resume_research(run_id: UUID,user: UserPublic = Depends(mutation_user)):
        repo.expire_abandoned()
        return repo.resume_for_user(str(run_id),str(user.id),settings)

    @app.get('/research/{run_id}/events',responses=ERRORS)
    async def research_events(run_id: UUID,request: Request,user: UserPublic = Depends(current_user)):
        from starlette.concurrency import run_in_threadpool
        from deepresearch_ai.api.events import progress_events
        await run_in_threadpool(get_run,run_id,str(user.id))
        return StreamingResponse(progress_events(request,repo,current_user,str(user.id),str(run_id),settings.progress_stream_seconds),
                                 media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

    @app.get('/research', response_model=RunPage, responses=ERRORS)
    def list_research(limit: Annotated[int, Query(ge=1, le=100)] = 20,
                      offset: Annotated[int, Query(ge=0)] = 0, user: UserPublic = Depends(current_user),
                      q: Annotated[str, Query(max_length=200)] = '', status: RunStatus | None = None,
                      depth: ResearchDepth | None = None, created_from: date | None = None, created_to: date | None = None):
        if created_to == date.max or (created_from and created_to and created_from > created_to):
            raise HTTPException(422, detail={'code':'invalid_date_range', 'message':'Choose a valid inclusive date range.'})
        repo.expire_abandoned()
        return repo.list_for_user(str(user.id), limit, offset, query=q, status=status, depth=depth,
            created_from=datetime.combine(created_from, time.min, timezone.utc) if created_from else None,
            created_to=datetime.combine(created_to + timedelta(days=1), time.min, timezone.utc) if created_to else None)

    @app.delete('/research/{run_id}', status_code=204, responses=ERRORS)
    def delete_research(run_id: UUID, user: UserPublic = Depends(mutation_user)):
        if not repo.delete_for_user(str(run_id), str(user.id)):
            raise HTTPException(404, detail={'code':'run_not_found', 'message':'Research run not found.'})

    @app.get('/research/{run_id}', response_model=RunDetail, responses=ERRORS)
    def read_research(run_id: UUID, user: UserPublic = Depends(current_user)):
        return get_run(run_id, str(user.id))

    @app.get('/research/{run_id}/sources', response_model=list[SourceDocument], responses=ERRORS)
    def read_sources(run_id: UUID, user: UserPublic = Depends(current_user)):
        get_run(run_id, str(user.id))
        return repo.sources_for_user(str(run_id), str(user.id))

    @app.get('/research/{run_id}/report', response_model=ResearchReport, responses=ERRORS)
    def read_report(run_id: UUID, user: UserPublic = Depends(current_user)):
        run = get_run(run_id, str(user.id))
        if run.status != RunStatus.completed or run.report is None:
            raise HTTPException(409, detail={'code': 'report_not_ready', 'message': 'A completed report is not available for this run.'})
        return run.report

    @app.get('/research/{run_id}/report.md', responses=ERRORS,
             response_class=Response, description='Download a sanitized Markdown report and cited sources.')
    def download_report(run_id: UUID, user: UserPublic = Depends(current_user)):
        report = read_report(run_id, user)
        return Response(report_markdown(report), media_type='text/markdown; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="research-{run_id}.md"'})

    @app.post('/research/{run_id}/cancel', response_model=RunDetail, responses=ERRORS)
    def cancel_research(run_id: UUID, user: UserPublic = Depends(mutation_user)):
        run = get_run(run_id, str(user.id))
        if run.status == RunStatus.cancelled:
            return run
        if not repo.cancel_for_user(str(run_id), str(user.id)):
            raise HTTPException(409, detail={'code': 'run_not_cancellable', 'message': 'Only queued or running research can be cancelled.'})
        return get_run(run_id, str(user.id))

    # API routes take precedence. A repository build can share this origin,
    # keeping browser requests and downloads free of CORS/key configuration.
    if (settings.frontend_dist_dir / 'index.html').is_file():
        app.mount('/', StaticFiles(directory=settings.frontend_dist_dir, html=True), name='dashboard')
    return app
