"""원장 사건의 삽입과 조회.

제어 사건은 상태를 바꾼 같은 트랜잭션에서 한 건씩 삽입한다. 상태 버전은 데이터베이스 트리거가
올리므로 호출자는 변경 뒤 행에서 읽은 값을 넘긴다.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from mint_litellm.control.db.connection import fetchall
from mint_litellm.types.events import Event, EventKind, control_source_key


def record(
    conn: psycopg.Connection,
    instance_id: str,
    kind: EventKind,
    *,
    entity_id: str,
    state_version: int,
    actor: str,
    payload: dict[str, Any],
    subject: dict[str, Any] | None = None,
) -> Event:
    event = Event(
        instance_id=instance_id,
        inserted_at=datetime.now(UTC),
        kind=kind,
        source_key=control_source_key(kind, str(entity_id), state_version),
        actor=actor,
        subject=subject,
        payload=payload,
    )
    conn.execute(
        "INSERT INTO mint.events (event_id, instance_id, kind, source_key, actor, subject, payload)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            event.event_id,
            instance_id,
            kind.value,
            event.source_key,
            actor,
            None if subject is None else Jsonb(event.subject),
            Jsonb(event.payload),
        ),
    )
    return event


def list_events(
    conn: psycopg.Connection,
    instance_id: str,
    *,
    kind: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    invocation_id: UUID | None = None,
    limit: int = 100,
) -> list[dict]:
    clauses = ["instance_id = %s"]
    params: list[Any] = [instance_id]
    for column, value in (
        ("kind = %s", kind),
        ("inserted_at >= %s", since),
        ("inserted_at < %s", until),
        ("invocation_id = %s", invocation_id),
    ):
        if value is not None:
            clauses.append(column)
            params.append(value)
    params.append(limit)
    return fetchall(
        conn,
        "SELECT event_id, inserted_at, kind, source_key, invocation_id, attempt_id, actor,"
        " subject, payload FROM mint.events WHERE " + " AND ".join(clauses)
        + " ORDER BY inserted_at DESC, event_id DESC LIMIT %s",
        tuple(params),
    )



