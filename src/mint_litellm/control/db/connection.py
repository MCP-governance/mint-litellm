"""연결 보조 함수. 행은 항상 사전으로 읽는다."""

from typing import Any

import psycopg
from psycopg.rows import dict_row


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url)


def fetchone(conn: psycopg.Connection, sql: str, params: tuple[Any, ...] = ()) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def fetchall(conn: psycopg.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()
