"""작업 실행기 ``mint-litellm worker``.

작업 실행기는 ``mint_worker_app`` 로그인으로 접속하고, 일정을 작업으로 등록하고, 임대가 만료된
작업을 회수하고, 대기 작업을 하나씩 임대해 처리기를 부른다. 복제본이 여럿이어도 임대와 일정
등록이 ``FOR UPDATE SKIP LOCKED``와 멱등 키로 한 번씩만 일어난다.
"""

import logging
import os
import signal
import socket
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from typing import Any

import psycopg

from mint_litellm.control import jobs
from mint_litellm.control.config import ControlConfig
from mint_litellm.control.db.connection import connect, fetchone
from mint_litellm.control.errors import ControlError
from mint_litellm.control.stage2_config import Stage2Settings

log = logging.getLogger("mint_litellm.worker")

POLL_SECONDS = 1.0
# 되돌림 상태에서 외부 쓰기 작업을 다시 시도하기까지의 간격(초)
RESTORE_BLOCKED_RETRY_SECONDS = 10


def no_restore_detected(conn: psycopg.Connection, instance_id: str) -> bool:
    """되돌림 판정(후속 단계). 지금은 판정 자체가 없어 언제나 「정상」으로 본다.

    후속 단계가 이 함수를 대신한다. 되돌림 상태이면 참을 돌려준다.
    """
    return False


class Worker:
    def __init__(
        self,
        database_url: str,
        instance_id: str,
        settings: Stage2Settings,
        *,
        lease_seconds: float | None = None,
        restore_check: Callable[[psycopg.Connection, str], bool] = no_restore_detected,
        owner: str | None = None,
        litellm: Any = None,
    ) -> None:
        self.database_url = database_url
        self.instance_id = instance_id
        self.settings = settings
        self.lease_seconds = lease_seconds or settings.worker_lease_seconds
        self.restore_check = restore_check
        self.litellm = litellm
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.stop = threading.Event()
        self.current: jobs.JobContext | None = None
        self.conn: psycopg.Connection | None = None
        self._beat_conn: psycopg.Connection | None = None

    # --- 수명 ---------------------------------------------------------------

    def open(self) -> None:
        self.conn = connect(self.database_url)
        self.conn.autocommit = True
        self._beat_conn = connect(self.database_url)
        self._beat_conn.autocommit = True
        if fetchone(self.conn, "SELECT to_regclass('mint.jobs') IS NOT NULL AS ok")["ok"] is False:
            raise ControlError("2단계 스키마가 적용되어 있지 않습니다. init을 실행하십시오.")
        member = fetchone(
            self.conn, "SELECT pg_has_role(current_user, 'mint_worker', 'USAGE') AS ok"
        )
        if not member["ok"]:
            raise ControlError(
                "작업 실행기는 mint_worker_app 로그인(mint_worker 그룹)으로 접속해야 합니다."
            )
        jobs.ensure_schedules(self.conn, self.instance_id, self.settings)

    def close(self) -> None:
        for conn in (self.conn, self._beat_conn):
            if conn is not None:
                conn.close()
        self.conn = self._beat_conn = None

    def request_stop(self) -> None:
        """종료 신호: 현재 작업에 취소를 알리고(처리기가 끝내거나 임대를 놓는다) 반복을 끝낸다."""
        self.stop.set()
        if self.current is not None:
            self.current.cancel.set()

    # --- 한 번의 반복 ---------------------------------------------------------

    def step(self) -> bool:
        """일정 등록, 만료 회수, 작업 하나 처리. 작업을 처리했으면 참이다."""
        assert self.conn is not None
        jobs.register_due_schedules(self.conn, self.instance_id)
        jobs.reclaim_expired(self.conn, self.instance_id)
        job = jobs.lease(
            self.conn, self.instance_id, self.owner, self.lease_seconds, sorted(jobs.HANDLERS)
        )
        if job is None:
            return False
        self._run(job)
        return True

    def run_forever(self, poll_seconds: float = POLL_SECONDS) -> None:
        while not self.stop.is_set():
            if not self.step():
                self.stop.wait(poll_seconds)

    # --- 작업 하나 ------------------------------------------------------------

    def _beat(self, ctx: jobs.JobContext, finished: threading.Event) -> None:
        """임대 1/3마다 연장한다. 연장이 실패하면 임대를 잃은 것이므로 작업에 취소를 알린다."""
        assert self._beat_conn is not None
        while not finished.wait(self.lease_seconds / 3):
            try:
                alive = jobs.extend(self._beat_conn, ctx.job["id"], self.owner, self.lease_seconds)
            except psycopg.Error:
                alive = False  # 연장 결과를 알 수 없으면 외부 호출을 멈추는 쪽이 안전하다
            if not alive:
                ctx.lost.set()
                ctx.cancel.set()
                return

    def _run(self, job: dict) -> None:
        assert self.conn is not None
        conn, job_id = self.conn, job["id"]
        restored = self.restore_check(conn, self.instance_id)
        if restored and jobs.KINDS[job["kind"]].external_write:
            jobs.release(conn, job_id, self.owner, "restore_blocked", RESTORE_BLOCKED_RETRY_SECONDS)
            return
        ctx = jobs.JobContext(conn, self.instance_id, job, self.settings, self.litellm)
        self.current = ctx
        if self.stop.is_set():
            ctx.cancel.set()
        finished = threading.Event()
        beat = threading.Thread(target=self._beat, args=(ctx, finished), daemon=True)
        beat.start()
        outcome: str | tuple[str, str] = "ok"
        try:
            jobs.HANDLERS[job["kind"]](ctx)
        except jobs.Cancelled:
            outcome = "cancelled"
        except Exception as error:  # 어떤 실패도 작업 실패로 센다(메시지는 저장하지 않는다)
            code = str(getattr(error, "code", None) or type(error).__name__)[:100]
            log.warning("작업 %s(%s) 실패: %s", job["kind"], job_id, code)
            outcome = ("error", code)
        finally:
            finished.set()
            beat.join()
            self.current = None
        if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            conn.rollback()
        if ctx.lost.is_set():
            log.warning("작업 %s(%s)의 임대를 잃어 결과를 기록하지 않습니다.", job["kind"], job_id)
        elif outcome == "ok":
            jobs.complete(conn, job_id, self.owner)
        elif outcome == "cancelled":
            jobs.release(conn, job_id, self.owner, "shutdown")
        else:
            jobs.fail(conn, job_id, self.owner, outcome[1])


def install_signals(worker: Worker) -> Callable[[], None]:
    """SIGTERM·SIGINT(Windows는 SIGBREAK도)로 종료한다. 이전 처리기를 되돌리는 함수를 돌려준다."""
    names = [n for n in ("SIGTERM", "SIGINT", "SIGBREAK") if hasattr(signal, n)]
    previous = {
        n: signal.signal(getattr(signal, n), lambda *_: worker.request_stop()) for n in names
    }

    def restore() -> None:
        for name, handler in previous.items():
            signal.signal(getattr(signal, name), handler)

    return restore


def main(environ: Mapping[str, str] | None = None) -> int:
    """``mint-litellm worker``. 필수 설정이 없으면 기동하지 않는다."""
    environ = dict(os.environ) if environ is None else dict(environ)
    config = ControlConfig.from_env(environ)
    settings = Stage2Settings.from_env(environ)
    litellm = config.litellm() if config.litellm_url and config.litellm_key else None
    worker = Worker(config.require_database(), config.instance_id, settings, litellm=litellm)
    worker.open()
    restore = install_signals(worker)
    print(f"작업 실행기 시작: {worker.owner}", file=sys.stderr, flush=True)
    try:
        worker.run_forever()
    finally:
        restore()
        worker.close()
    print("작업 실행기 종료", file=sys.stderr, flush=True)
    return 0
