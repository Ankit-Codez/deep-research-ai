"""Lease-fenced node checkpoints and provider receipts, without pickle or secrets."""
import hashlib
import json
import time
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import select, update

from deepresearch_ai.graph.state import ResearchState, ResearchUsage
from deepresearch_ai.services.recovery import CachedProviderFailure, ResearchPaused
from deepresearch_ai.storage.tables import CheckpointRow, ProviderCallRow, RunRow

VERSION = 1
STATE = TypeAdapter(ResearchState)


def frozen_settings(settings):
    return {key:value for key,value in settings.model_dump(mode='json').items()
            if key.startswith(('llm_','research_','provider_','gemini_thinking_'))
            or key in {'mock_mode','search_provider'}}


def resume_status(session, row):
    checkpoint = session.get(CheckpointRow,row.id)
    if row.status != 'failed' or checkpoint is None:
        return False, None
    if checkpoint.version != VERSION:
        return False, 'This checkpoint belongs to an incompatible workflow version.'
    if row.error_code == 'resume_incompatible':
        return False, 'This checkpoint does not match the deployed workflow. Start a new investigation explicitly.'
    pending = session.scalar(select(ProviderCallRow.id).where(ProviderCallRow.run_id == row.id,
        ProviderCallRow.kind.in_(['llm','search']),
        ProviderCallRow.status.in_(['pending','unknown'])).limit(1))
    if pending:
        return False, 'A provider request has an unknown outcome and may already be billed. Start a new investigation explicitly if needed.'
    return True, None


class DurableExecution:
    def __init__(self, repository, run_id, owner, settings):
        self.repo, self.run_id, self.owner = repository, run_id, owner
        self.settings = settings
        self.sequence, self.position = 0, 0

    def _fence(self, session):
        changed = session.execute(update(RunRow).where(*self.repo._owned(self.run_id,self.owner))
                                  .values(updated_at=datetime.now(timezone.utc)))
        if changed.rowcount != 1:
            raise ResearchPaused('Worker ownership was lost; no further provider request was sent.')

    def load(self):
        with self.repo.sessions() as session:
            checkpoint = session.get(CheckpointRow,self.run_id)
            if checkpoint is None:
                return None
            if checkpoint.version != VERSION:
                raise ResearchPaused('Checkpoint version is incompatible.')
            state = STATE.validate_json(checkpoint.state_json)
            state['started_at'] = time.monotonic()-state['usage'].elapsed_seconds
            self.sequence = checkpoint.sequence
            return checkpoint.node, checkpoint.phase, state, json.loads(checkpoint.settings_json)

    def checkpoint(self, phase, node, state):
        with self.repo.sessions.begin() as session:
            self._fence(session)
            row = session.get(CheckpointRow,self.run_id)
            if phase == 'started':
                # Reexecuting an interrupted node reuses its provider receipt positions.
                if row is None or row.phase == 'completed':
                    self.sequence = (row.sequence if row else 0)+1
                else:
                    self.sequence = row.sequence
                self.position = 0
            if row is None:
                row = CheckpointRow(run_id=self.run_id, settings_json=json.dumps(frozen_settings(self.settings)))
                session.add(row)
            row.version, row.sequence, row.node, row.phase = VERSION, self.sequence, node, phase
            row.state_json = STATE.dump_json(state).decode()
            row.updated_at = datetime.now(timezone.utc)

    @staticmethod
    def _restore(usage, saved):
        receipt = ResearchUsage.model_validate_json(saved)
        for key,value in receipt.model_dump().items():
            if isinstance(value,(int,float)) and key != 'elapsed_seconds':
                current = getattr(usage,key)
                setattr(usage,key,max(current or 0,value))

    def call(self, context, kind, operation, input_size, identity, encode, decode):
        from deepresearch_ai.services.runtime import StructuredOutputError
        self.position += 1
        request_hash = hashlib.sha256(identity.encode()).hexdigest()
        with self.repo.sessions.begin() as session:
            self._fence(session)
            call = session.scalar(select(ProviderCallRow).where(ProviderCallRow.run_id == self.run_id,
                ProviderCallRow.sequence == self.sequence, ProviderCallRow.position == self.position))
            if call:
                if call.request_hash != request_hash or call.kind != kind:
                    raise ResearchPaused('Saved provider request does not match this workflow. No request was repeated.',code='resume_incompatible')
                self._restore(context.usage,call.usage_json)
                saved_elapsed = ResearchUsage.model_validate_json(call.usage_json).elapsed_seconds
                elapsed = max(context.clock()-context.started_at,saved_elapsed)
                context.started_at = context.clock()-elapsed
                if call.status == 'completed':
                    return decode(call.result_json)
                if call.status == 'failed':
                    if call.failure_reason in StructuredOutputError.MESSAGES:
                        raise StructuredOutputError(call.failure_reason)
                    raise CachedProviderFailure(call.failure_status)
                if kind != 'fetch':
                    raise ResearchPaused('A provider request has an unknown outcome; it was not repeated.')
                # An interrupted ordinary page download can safely be attempted
                # again, but still consumes the original run's page/time budget.
                context.started_at -= context.settings.research_page_deadline
                context.admit(kind,input_size)
                call_id = call.id
                call.usage_json = context.usage.model_dump_json()
            else:
                context.admit(kind,input_size)
                call_id = str(uuid4())
                session.add(ProviderCallRow(id=call_id, run_id=self.run_id, sequence=self.sequence,
                    position=self.position, kind=kind, request_hash=request_hash, status='pending',
                    usage_json=context.usage.model_dump_json(), created_at=datetime.now(timezone.utc)))
        try:
            result = operation()
            payload = encode(result)
        except Exception as exc:
            # An explicit provider response is a known failure. Network/timeouts
            # and unclassified errors may have occurred after dispatch/billing.
            cause, seen, status = exc,set(),None
            while cause is not None and id(cause) not in seen:
                seen.add(id(cause))
                candidate = (getattr(cause,'status_code',None) or getattr(cause,'code',None)
                             or getattr(getattr(cause,'response',None),'status_code',None))
                if isinstance(candidate,int):
                    status = candidate
                    break
                cause = cause.__cause__ or cause.__context__
            status = status if isinstance(status,int) else None
            reason = exc.reason if isinstance(exc,StructuredOutputError) else None
            known = kind == 'fetch' or status is not None or reason is not None
            context.usage.elapsed_seconds = max(context.usage.elapsed_seconds,context.clock()-context.started_at)
            self._receipt(call_id,context.usage,'failed' if known else 'unknown',status_code=status,reason=reason)
            if not known:
                from deepresearch_ai.services.runtime import provider_failure_message
                diagnosis = provider_failure_message(exc,'Web search' if kind == 'search' else 'LLM request')
                raise ResearchPaused(diagnosis+' Its outcome is unknown and it may have been billed; automatic replay was blocked.') from None
            raise
        context.usage.elapsed_seconds = max(context.usage.elapsed_seconds,context.clock()-context.started_at)
        self._receipt(call_id,context.usage,'completed',payload=payload)
        return result

    def _receipt(self, call_id, usage, status, *, payload=None, status_code=None, reason=None):
        # Only safe status/reason fields are saved, never raw SDK exception text.
        with self.repo.sessions.begin() as session:
            self._fence(session)
            session.execute(update(ProviderCallRow).where(ProviderCallRow.id == call_id).values(
                status=status, result_json=payload, failure_status=status_code,
                failure_reason=reason, usage_json=usage.model_dump_json()))
