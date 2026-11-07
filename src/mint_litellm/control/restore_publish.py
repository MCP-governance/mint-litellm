"""작업 ``restore_publish``: 복원 요청의 파일을 게시한다(해제 경로).

되돌림 차단 상태에서도 이 작업만은 실행된다. 그래서 게시 직전에 요청이 정당한지 다시 확인한다
(요청 행과 작업 행만 직접 넣어 차단을 풀 수 없다). 순서: 확인 -> 기존 파일을
``restored/<요청>/``로 보관 -> 새 부정 상태·체크포인트 파일 게시와 검증 -> ``latest.json``은 맨
마지막 -> 요청 ``published``. 중간에 죽거나 임대를 잃으면 ``latest.json``이 없어 차단이 유지되고
다시 실행하면 이어서 끝낸다. 같은 인스턴스의 게시는 자문 잠금으로 한 번에 하나다.
"""

import shutil
from pathlib import Path
from typing import Any

import psycopg

from mint_litellm.control import exports
from mint_litellm.control.db.connection import fetchone
from mint_litellm.control.errors import ControlError
from mint_litellm.control.jobs import JobContext
from mint_litellm.control.services import restore as restore_service
from mint_litellm.restore import files, judge
from mint_litellm.types.events import EventKind

_MARKER = ".archived"


def _dest(settings, instance_id: str, request_id: str) -> Path:
    return settings.export_dir / instance_id / "restored" / request_id


def archive(settings, instance_id: str, request_id: str) -> None:
    """기존 ``latest.json``과 ``negative/``를 지우지 않고 ``restored/<요청>/``로 옮긴다."""
    root = settings.export_dir / instance_id
    dest = _dest(settings, instance_id, request_id)
    if (dest / _MARKER).exists():
        return  # 이미 보관했다(재실행)
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("latest.json", "negative"):
        source = root / name
        if source.exists():
            shutil.move(str(source), str(dest / name))
    (dest / _MARKER).write_text(files.now(), encoding="utf-8")


def _event(conn: psycopg.Connection, iid: str, kind: EventKind, request_id: str):
    return fetchone(
        conn,
        "SELECT actor, payload FROM mint.events WHERE instance_id = %s AND kind = %s"
        " AND payload ->> 'request_id' = %s",
        (iid, kind.value, request_id),
    )


def _two_admins(conn: psycopg.Connection, iid: str, first: str, second: str | None) -> bool:
    if not second or first == second:
        return False
    rows = {
        name: fetchone(
            conn,
            "SELECT role, user_id FROM mint.operators WHERE instance_id = %s AND name = %s",
            (iid, name),
        )
        for name in (first, second)
    }
    a, b = rows[first], rows[second]
    return bool(
        a and b and a["role"] == b["role"] == "admin" and a["user_id"] and b["user_id"]
        and a["user_id"] != b["user_id"]
    )


def _original_items(settings, iid: str, request_id: str, archived: bool) -> list[dict]:
    """복원 대상이었던 부정 상태 항목: 보관했다면 보관본에서, 아니면 현재 파일에서 읽는다."""
    keys = exports.keys(settings)
    if archived:
        folder = _dest(settings, iid, request_id) / "negative"
        names = [p for p in folder.glob("*.json") if p.stem.isdigit()] if folder.is_dir() else []
        path = max(names, key=lambda p: int(p.stem), default=None)
    else:
        path = files.newest_negative(settings.export_dir, iid)
    if path is None:
        return []
    return files.read_verified(path, keys).get("items") or []


def _anchor_ok(conn, iid: str, settings, request_id: str, archived: bool) -> bool:
    """원본 ``latest.json``의 봉인 끝이 DB의 ``seal_batches``와 같은가. 읽지 못하면 거짓이다."""
    path = (
        _dest(settings, iid, request_id) / "latest.json"
        if archived
        else files.latest_path(settings.export_dir, iid)
    )
    try:
        latest = files.read_verified(path, exports.keys(settings))
    except (FileNotFoundError, files.ExportUnverified):
        return False
    seq = latest.get("to_seal_seq")
    if seq is None:
        return True
    row = fetchone(
        conn,
        "SELECT encode(tail_hash, 'hex') AS tail FROM mint.seal_batches"
        " WHERE instance_id = %s AND to_seal_seq = %s",
        (iid, seq),
    )
    return row is not None and row["tail"] == latest.get("tail_hash")


def validate(job: JobContext, request: dict[str, Any]) -> None:
    """게시 직전에 요청이 정당한지 다시 확인한다. 아니면 ``ControlError``."""
    conn, iid, settings = job.conn, job.instance_id, job.settings
    request_id = str(request["id"])
    archived = (_dest(settings, iid, request_id) / _MARKER).exists()
    if request["kind"] == "attest":
        event = _event(conn, iid, EventKind.RESTORE_ATTESTED, request_id)
        epoch = fetchone(
            conn, "SELECT coalesce(max(epoch), 0) AS e FROM mint.state_epoch"
            " WHERE instance_id = %s", (iid,))["e"]
        ok = (
            event is not None
            and event["actor"] == request["requested_by"]
            and event["payload"].get("file_epoch") == request["file_epoch"]
            and request["file_epoch"] is not None
            and request["target_epoch"] > request["file_epoch"]
            and epoch >= request["target_epoch"]
        )
        if ok:
            items = _original_items(settings, iid, request_id, archived)
            ok = not restore_service.compare(conn, iid, items)
        if ok and not _anchor_ok(conn, iid, settings, request_id, archived):
            # 요청의 cause 값을 믿지 않고 원본 latest.json 기준으로 다시 계산한 결과다
            ok = _two_admins(conn, iid, request["requested_by"], request["second_approver"])
    else:
        event = _event(conn, iid, EventKind.RESTORE_STALE_ACCEPTED, request_id)
        ok = (
            event is not None
            and event["actor"] == request["requested_by"]
            and event["payload"].get("second_approver") == request["second_approver"]
            and _two_admins(conn, iid, request["requested_by"], request["second_approver"])
        )
        if ok and not archived:  # 파일이 실제로 없거나 검증에 실패한 상태여야 한다
            ok = judge.read_files(settings.export_dir, iid, exports.keys(settings)).error in (
                "missing", "unverified")
    if not ok:
        raise ControlError("복원 요청이 원장·DB 상태와 맞지 않아 게시하지 않습니다.")


def publish(job: JobContext) -> None:
    conn, iid, settings = job.conn, job.instance_id, job.settings
    request = fetchone(
        conn, "SELECT * FROM mint.restore_requests WHERE instance_id = %s AND id::text = %s",
        (iid, job.job["target_ref"]),
    )
    if request is None:
        raise ControlError("복원 요청이 없습니다.")
    if request["status"] == "published":
        return
    lock = f"mint.restore_publish:{iid}"
    got = fetchone(conn, "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (lock,))["ok"]
    if not got:
        raise exports.ExportFailed("다른 실행기가 복원 파일을 게시하는 중입니다.")
    try:
        job.checkpoint()
        validate(job, request)
        archive(settings, iid, str(request["id"]))
        job.checkpoint()
        exports.write_export(
            conn, iid, settings, "negative_state", actor="worker", write_latest=False,
            republish=True,
        )
        job.checkpoint()
        checkpoint = exports.write_export(
            conn, iid, settings, "checkpoint", actor="worker", write_latest=False
        )
        job.checkpoint()
        # latest.json은 맨 마지막이다: 그 전에 죽으면 파일이 없어 차단이 유지된다.
        exports.write_latest_file(settings, iid, checkpoint, force=True)
        state = judge.read_files(settings.export_dir, iid, exports.keys(settings))
        if state.error is not None or state.latest is None:
            raise exports.ExportFailed("게시한 파일을 다시 읽어 검증하지 못했습니다.")
        conn.execute(
            "UPDATE mint.restore_requests SET status = 'published', published_at = now()"
            " WHERE id = %s AND status = 'pending'", (request["id"],),
        )
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock,))
