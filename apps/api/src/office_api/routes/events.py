"""Event replay (GET) and realtime stream (WS), both project-scoped (spec §10-11).

WebSocket protocol:
- Connect to ``/projects/{id}/stream?after_seq=N&token=<bearer>`` (browsers cannot
  set Authorization on a WebSocket).
- Server replays every event with sequence > N, then pushes live events.
  Each message is one ``Event`` JSON object, in sequence order, without gaps.
- Close codes: 4401 unauthenticated, 4404 project not found / no access,
  4409 client too slow; reconnect with ``after_seq`` = last sequence received.
"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from office_contracts import Event
from pydantic import BaseModel

from office_api.auth import Membership, Session, authenticate, find_membership
from office_api.events import fetch_events

router = APIRouter(prefix="/projects/{project_id}", tags=["events"])

REPLAY_BATCH = 500


class EventPage(BaseModel):
    events: list[Event]
    next_after_seq: int


@router.get("/events", response_model=EventPage)
async def list_events(
    project_id: str,
    member: Membership,
    session: Session,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
):
    events = await fetch_events(session, project_id, after_seq, limit)
    return EventPage(events=events, next_after_seq=events[-1].sequence if events else after_seq)


@router.websocket("/stream")
async def stream(
    websocket: WebSocket,
    project_id: str,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    token: str | None = None,
):
    sessionmaker = websocket.app.state.sessionmaker
    await websocket.accept()
    async with sessionmaker() as session:
        principal = await authenticate(session, token)
        if principal is None:
            await websocket.close(code=4401, reason="unauthenticated")
            return
        if await find_membership(session, project_id, principal.id) is None:
            await websocket.close(code=4404, reason="project_not_found")
            return

    broker = websocket.app.state.broker
    # Subscribe before replaying so nothing committed in between is missed;
    # duplicates are dropped by sequence.
    sub = broker.subscribe(project_id)
    receiver = asyncio.create_task(_wait_for_disconnect(websocket))
    try:
        last = await _send_from_db(websocket, sessionmaker, project_id, after_seq)
        while True:
            getter = asyncio.create_task(sub.queue.get())
            done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                getter.cancel()
                return
            event = getter.result()
            if event is None:
                await websocket.close(code=4409, reason="resync")
                return
            if event.sequence <= last:
                continue
            if event.sequence > last + 1:
                # Publishes can arrive out of order; the gap is already committed.
                last = await _send_from_db(websocket, sessionmaker, project_id, last)
                if event.sequence <= last:
                    continue
            await websocket.send_json(event.model_dump(mode="json"))
            last = event.sequence
    except WebSocketDisconnect:
        return
    finally:
        receiver.cancel()
        broker.unsubscribe(sub)


async def _send_from_db(websocket: WebSocket, sessionmaker, project_id: str, after: int) -> int:
    last = after
    while True:
        async with sessionmaker() as session:
            events = await fetch_events(session, project_id, last, REPLAY_BATCH)
        for event in events:
            await websocket.send_json(event.model_dump(mode="json"))
            last = event.sequence
        if len(events) < REPLAY_BATCH:
            return last


async def _wait_for_disconnect(websocket: WebSocket) -> None:
    # Client-to-server messages are ignored in Phase 0; this only detects disconnect.
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        return
