"""경보 생성·해결과 웹훅 전송 대기 등록.

모든 경보 종류는 ``raise_alert``로 만들고 ``resolve``로 닫는다. 같은 ``dedup_key``의 미해결 경보가
있으면 새로 만들지 않고 ``count``와 ``last_at``만 올린다. 웹훅 전송 행은 경보가 새로 열린 뒤
설정된 주소마다 하나씩 만들고, 전송 작업의 멱등 키는 전송 식별자로 정해진다.
"""

import hashlib
from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from mint_litellm.control import ledger
from mint_litellm.control.db.connection import fetchall
from mint_litellm.types.events import EventKind
from mint_litellm.types.ids import new_uuid7

# 자동으로 일어난 경보 전이의 행위자(운영자 토큰이 없는 작업 실행기)
AUTO_ACTOR = "mint-worker"

# 한 번의 등록 호출이 다루는 경보 수 상한(작업 실행기 반복 한 번 안에서 끝난다)
DELIVERY_BATCH = 100


def job_failed_key(kind: str, target_ref: str) -> str:
    return f"job_failed:{kind}:{target_ref}"[:512]


def raise_alert(
    conn: psycopg.Connection,
    instance_id: str,
    *,
    kind: str,
    severity: str,
    dedup_key: str,
    subject: dict[str, Any],
) -> bool:
    """미해결 경보가 있으면 횟수와 마지막 시각만 올리고, 없으면 새로 만든다. 새로 만들었으면 참.

    새로 만든 행마다 ``alert_opened``(순번 0)를 같은 트랜잭션에 남긴다.
    """
    row = conn.execute(
        "INSERT INTO mint.alerts (id, instance_id, kind, severity, dedup_key, subject)"
        " VALUES (%s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (instance_id, dedup_key) WHERE status <> 'resolved'"
        " DO UPDATE SET count = mint.alerts.count + 1, last_at = now()"
        " RETURNING id, (xmax = 0) AS created",
        (new_uuid7(), instance_id, kind, severity, dedup_key, Jsonb(subject)),
    ).fetchone()
    created = bool(row[1])
    if created:
        ledger.record(
            conn,
            instance_id,
            EventKind.ALERT_OPENED,
            entity_id=str(row[0]),
            state_version=0,
            actor=AUTO_ACTOR,
            payload={"kind": kind, "severity": severity},
        )
    return created


def resolve(conn: psycopg.Connection, instance_id: str, dedup_key: str) -> int:
    """같은 키의 미해결 경보를 닫고 ``alert_resolved``(순번 2)를 남긴다. 닫은 수를 돌려준다."""
    rows = conn.execute(
        "UPDATE mint.alerts SET status = 'resolved', resolved_at = now()"
        " WHERE instance_id = %s AND dedup_key = %s AND status <> 'resolved' RETURNING id, kind",
        (instance_id, dedup_key),
    ).fetchall()
    for alert_id, kind in rows:
        record_resolved(conn, instance_id, str(alert_id), kind, AUTO_ACTOR)
    return len(rows)


def record_resolved(
    conn: psycopg.Connection, instance_id: str, alert_id: str, kind: str, actor: str
) -> None:
    ledger.record(
        conn,
        instance_id,
        EventKind.ALERT_RESOLVED,
        entity_id=alert_id,
        state_version=2,
        actor=actor,
        payload={"kind": kind},
    )


def channel_for(url: str) -> str:
    """전송 행의 채널. 주소는 데이터베이스에 두지 않고 해시만 둔다(주소에 비밀이 있을 수 있다)."""
    return "webhook:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def enqueue_deliveries(conn: psycopg.Connection, instance_id: str, urls: Sequence[str]) -> int:
    """아직 전송 행이 없는 미해결 경보마다 주소별 전송 행과 ``alert_deliver`` 작업을 만든다.

    작업 등록은 전송 행과 같은 문장 묶음이며, 멱등 키 ``alert_deliver:<delivery_id>``로 중복이 없다.
    """
    made = 0
    for url in urls:
        channel = channel_for(url)
        pending = fetchall(
            conn,
            "SELECT a.id FROM mint.alerts a WHERE a.instance_id = %s AND a.status <> 'resolved'"
            " AND NOT EXISTS (SELECT 1 FROM mint.alert_deliveries d"
            "  WHERE d.instance_id = a.instance_id AND d.alert_id = a.id AND d.channel = %s)"
            " ORDER BY a.first_at LIMIT %s",
            (instance_id, channel, DELIVERY_BATCH),
        )
        for row in pending:
            delivery_id = new_uuid7()
            conn.execute(
                "INSERT INTO mint.alert_deliveries"
                " (id, instance_id, alert_id, channel, delivery_id) VALUES (%s, %s, %s, %s, %s)",
                (new_uuid7(), instance_id, row["id"], channel, delivery_id),
            )
            conn.execute(
                "INSERT INTO mint.jobs (id, instance_id, kind, target_ref, idempotency_key)"
                " VALUES (%s, %s, 'alert_deliver', %s, %s)"
                " ON CONFLICT (instance_id, idempotency_key) DO NOTHING",
                (
                    new_uuid7(),
                    instance_id,
                    str(delivery_id),
                    f"alert_deliver:{delivery_id}",
                ),
            )
            made += 1
    return made
