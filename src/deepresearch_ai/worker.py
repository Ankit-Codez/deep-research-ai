"""Standalone durable-queue worker. Run separately from the FastAPI web server."""
import argparse
import logging
from threading import Event, Thread
from uuid import uuid4

from deepresearch_ai.config import ConfigurationError, Settings
from deepresearch_ai.config.logging import configure_logging
from deepresearch_ai.models import ResearchRequest
from deepresearch_ai.services.depth import settings_for_depth
from deepresearch_ai.services.workflow import GraphResearchService
from deepresearch_ai.storage.base import RunRepository
from deepresearch_ai.storage.sql import SQLRunRepository
from deepresearch_ai.storage.recovery import DurableExecution
from deepresearch_ai.services.recovery import ResearchPaused

logger = logging.getLogger(__name__)


class RunInterrupted(RuntimeError):
    """Cancellation or loss of the lease prevents further graph steps/commits."""


class ResearchWorker:
    def __init__(self, repository: RunRepository, settings: Settings, *, service_factory=GraphResearchService):
        self.repository, self.settings = repository, settings
        self.service_factory = service_factory

    def run_once(self) -> bool:
        """Process one queued run. False means idle. No automatic paid-call replay."""
        repo = self.repository
        repo.expire_abandoned()
        owner = str(uuid4())
        run = repo.claim_next(owner, self.settings.worker_lease_seconds)
        if run is None:
            return False
        run_id = str(run.id)
        stop, lost = Event(), Event()
        settings = settings_for_depth(self.settings, run.depth)
        if run.token_budget is not None:
            updates = {'research_max_tokens':min(settings.research_max_tokens, run.token_budget), **run.cost_rates}
            if run.cost_budget_usd is not None:
                updates['research_max_cost_usd'] = min(settings.research_max_cost_usd or run.cost_budget_usd,
                                                        run.cost_budget_usd)
            settings = settings.model_copy(update=updates)

        def heartbeat():
            while not stop.wait(self.settings.worker_lease_seconds / 3):
                try:
                    if not repo.renew(run_id, owner, self.settings.worker_lease_seconds):
                        lost.set()
                        break
                except Exception:
                    lost.set()
                    break

        thread = Thread(target=heartbeat, name='research-lease', daemon=True)
        thread.start()
        progress, step, iteration = run.progress, run.current_step or 'planner', run.iteration
        statuses, sources = dict(run.step_status), repo.sources(run_id)
        usage = {}
        steps_started = 0

        def save_progress():
            if lost.is_set() or not repo.update_progress(run_id, owner, step, progress, iteration, statuses, sources, usage):
                raise RunInterrupted('Run cancelled or worker lease lost.')

        def on_step(name, current_iteration):
            nonlocal progress, step, iteration, steps_started
            step, iteration = name, current_iteration
            steps_started += 1
            progress = max(progress,min(.95, steps_started / (5 + 4 * settings.research_max_retries + 2 * settings.research_max_source_retries)))
            save_progress()

        def on_update(state):
            nonlocal iteration, statuses, sources, usage
            iteration, statuses, sources = state['iteration'], dict(state['step_status']), list(state['sources'])
            usage = state['usage'].model_dump()
            save_progress()

        try:
            service = self.service_factory(settings, on_step=on_step, on_update=on_update)
            if isinstance(service,GraphResearchService):
                service.persistence = DurableExecution(repo,run_id,owner,settings)
            report = service.run(ResearchRequest(query=run.topic, instructions=run.instructions,
                                                 max_sources=settings.research_max_pages))
            if not lost.is_set():
                repo.finish(run_id, owner, report)
        except ResearchPaused as exc:
            repo.fail(run_id,owner,str(exc),code=exc.code)
        except RunInterrupted:
            # A cancellation or expiry already owns the terminal state. Fenced writes
            # ensure a late synchronous provider response cannot overwrite it.
            repo.fail(run_id, owner, 'Worker ownership was lost; research was stopped.')
        except ConfigurationError:
            repo.fail(run_id, owner, 'Research could not start. Check provider credentials and worker configuration.',code='provider_configuration')
        except Exception as exc:
            logger.error('Research run %s failed (%s).', run_id, type(exc).__name__)
            repo.fail(run_id, owner, 'Research execution failed. Check the worker configuration and try a new run.')
        finally:
            stop.set()
            thread.join(timeout=2)
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(description='Process durable DeepResearch jobs outside HTTP requests.')
    parser.add_argument('--once', action='store_true', help='Process at most one queued job, then exit.')
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level)
    repository = SQLRunRepository(settings.database_url.get_secret_value())
    repository.initialize()
    worker = ResearchWorker(repository, settings)
    idle = Event()
    try:
        while True:
            worked = worker.run_once()
            if args.once:
                return 0
            if not worked:
                idle.wait(settings.worker_poll_seconds)
    except KeyboardInterrupt:
        logger.info('Worker stopped; unfinished runs expire according to their lease.')
        return 0
    finally:
        repository.close()


if __name__ == '__main__':
    raise SystemExit(main())
