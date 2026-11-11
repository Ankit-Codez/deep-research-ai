"""Bounded SSE connections read durable progress; polling remains a fallback."""
import asyncio
import hashlib
import json
import time

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool


def event(name,data,event_id=None):
    prefix = f'id: {event_id}\n' if event_id else ''
    return prefix+f'event: {name}\ndata: {json.dumps(data,default=str)}\n\n'


async def progress_events(request, repository, current_user, user_id, run_id, duration):
    deadline = time.monotonic()+duration
    last = request.headers.get('last-event-id','')
    yield 'retry: 1500\n\n'
    while time.monotonic() < deadline:
        if await request.is_disconnected():
            return
        try:
            user = await run_in_threadpool(current_user,request)
            if str(user.id) != user_id:
                yield event('auth_required',{'code':'authentication_required'})
                return
            await run_in_threadpool(repository.expire_abandoned)
            run = await run_in_threadpool(repository.get_for_user,run_id,user_id)
            if run is None:
                yield event('deleted',{'id':run_id})
                return
            data = run.model_dump(mode='json',exclude={'report','cost_rates','token_budget','cost_budget_usd'})
            cursor = hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()[:24]
            terminal = run.status in {'completed','failed','cancelled'}
            if cursor != last or terminal:
                yield event('progress',data,cursor)
                last = cursor
            if terminal:
                yield event('end',{'status':run.status})
                return
            yield ': heartbeat\n\n'
            await asyncio.sleep(.5)
        except HTTPException:
            yield event('auth_required',{'code':'authentication_required'})
            return
        except Exception:
            yield event('unavailable',{'code':'progress_unavailable'})
            return
