"""Event store + in-process broker (Phase 0 prototype; Redis Streams later, ADR-0002).

Write path: ``append_event`` inside the caller's transaction, then
``commit_and_publish``. Events are published only after commit, so a live
subscriber never sees an event that could still roll back (BR-09).
"""

import asyncio
from collections import defaultdict
from typing import Any

from office_contracts import Event, EventType
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from office_api.db import new_id, utcnow
from office_api.models import EventRow, Project

_PENDING = "pending_events"


def to_contract(row: EventRow) -> Event:
    return Event(
        event_id=row.id,
        project_id=row.project_id,
        sequence=row.seq,
        type=row.type,
        actor_id=row.actor_id,
        conversation_id=row.conversation_id,
        task_id=row.task_id,
        trace_id=row.trace_id,
        occurred_at=row.occurred_at,
        payload=row.payload,
    )


async def append_event(
    session: AsyncSession,
    *,
    project_id: str,
    type: EventType,
    actor_id: str,
    payload: dict[str, Any] | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    trace_id: str | None = None,
) -> Event:
    # Row-locks the project until commit, so sequences commit in order without gaps.
    seq = (
        await session.execute(
            update(Project)
            .where(Project.id == project_id)
            .values(event_seq=Project.event_seq + 1)
            .returning(Project.event_seq)
        )
    ).scalar_one()
    row = EventRow(
        id=new_id("evt"),
        project_id=project_id,
        seq=seq,
        type=EventType(type).value,
        actor_id=actor_id,
        conversation_id=conversation_id,
        task_id=task_id,
        trace_id=trace_id,
        payload=payload or {},
        occurred_at=utcnow(),
    )
    session.add(row)
    event = to_contract(row)
    session.info.setdefault(_PENDING, []).append(event)
    return event


async def commit_and_publish(session: AsyncSession, broker: "EventBroker") -> None:
    pending: list[Event] = session.info.pop(_PENDING, [])
    await session.commit()
    for event in pending:
        broker.publish(event)


async def fetch_events(
    session: AsyncSession,
    project_id: str,
    after_seq: int,
    limit: int,
    *,
    types: list[str] | None = None,
    task_id: str | None = None,
    actor_id: str | None = None,
    conversation_id: str | None = None,
) -> list[Event]:
    stmt = select(EventRow).where(EventRow.project_id == project_id, EventRow.seq > after_seq)
    if types:
        stmt = stmt.where(EventRow.type.in_(types))
    if task_id:
        stmt = stmt.where(EventRow.task_id == task_id)
    if actor_id:
        stmt = stmt.where(EventRow.actor_id == actor_id)
    if conversation_id:
        stmt = stmt.where(EventRow.conversation_id == conversation_id)
    rows = await session.scalars(stmt.order_by(EventRow.seq).limit(limit))
    return [to_contract(r) for r in rows]


class Subscription:
    def __init__(self, project_id: str, maxsize: int):
        self.project_id = project_id
        # None in the queue means "overflowed: reconnect and replay from last seq".
        self.queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=maxsize + 1)
        self.maxsize = maxsize
        self.overflowed = False


class EventBroker:
    def __init__(self, queue_size: int = 1000):
        self._queue_size = queue_size
        self._subs: dict[str, set[Subscription]] = defaultdict(set)

    def subscribe(self, project_id: str) -> Subscription:
        sub = Subscription(project_id, self._queue_size)
        self._subs[project_id].add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        subs = self._subs.get(sub.project_id)
        if subs is not None:
            subs.discard(sub)
            if not subs:
                del self._subs[sub.project_id]

    def publish(self, event: Event) -> None:
        for sub in list(self._subs.get(event.project_id, ())):
            if sub.overflowed:
                continue
            if sub.queue.qsize() >= sub.maxsize:
                sub.overflowed = True
                sub.queue.put_nowait(None)
            else:
                sub.queue.put_nowait(event)
