"""작업 ``restore_publish``: 복원 요청의 파일을 게시한다(해제 경로).

되돌림 차단 상태에서도 이 작업만은 실행된다. 그래서 게시 직전에 요청이 정당한지 다시 확인한다
(요청 행과 작업 행만 직접 넣어 차단을 풀 수 없다). 순서: 확인 -> 기존 파일을
``restored/<요청>/``로 보관 -> 새 부정 상태·체크포인트 파일 게시와 검증 -> ``latest.json``은 맨
마지막 -> 요청 ``published``. 중간에 죽거나 임대를 잃으면 ``latest.json``이 없어 차단이 유지되고
다시 실행하면 이어서 끝낸다. 같은 인스턴스의 게시는 자문 잠금으로 한 번에 하나다.

요청은 만들어진 시점의 ``latest.json`` 해시에 결속한다. 게시 직전의 해시가 다르면
요청을 거절하고 경보를 낸다. 게시를 끝내면 ``restored/<요청>/.done``을 남기고, 표지가 있는 요청은
DB에서 ``pending``으로 되살아나도 다시 게시하지 않는다(최신 증거를 옛 요청이 덮지 못한다).
"""

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from mint_litellm.control import alerts, exports
from mint_litellm.control.db.connection import fetchone
from mint_litellm.control.errors import ControlError
from mint_litellm.control.jobs import JobContext, PermanentFailure
from mint_litellm.control.services import restore as restore_service
from mint_litellm.restore import files, judge
from mint_litellm.types.events import EventKind

_MARKER = ".archived"  # 보관을 시작했다(이동 전에 쓴다)
_MOVED = ".moved"  # 이동을 끝냈다
_PUBLISHING = ".publishing"  # 새 파일 게시를 시작했다(latest.json을 쓰기 전에 남긴다)
_DONE = ".done"  # 게시와 검증을 모두 끝냈다(DB 확정 전에 죽어도 다시 게시하지 않는다)


def _dest(settings, instance_id: str, request_id: str) -> Path:
    return settings.export_dir / instance_id / "restored" / request_id


def archive(settings, instance_id: str, request_id: str) -> None:
    """기존 ``latest.json``과 ``negative/``를 지우지 않고 ``restored/<요청>/``로 옮긴다."""
    root = settings.export_dir / instance_id
    dest = _dest(settings, instance_id, request_id)
    if (dest / _MOVED).exists():
        return  # 이동까지 끝났다(재실행): 그 뒤에 쓴 새 파일은 건드리지 않는다
    dest.mkdir(parents=True, exist_ok=True)
    marker = dest / _MARKER
    if not marker.exists():  # 이동 전에 표식을 먼저 쓴다. 중간에 죽으면 다음 실행이 이어서 옮긴다.
        marker.write_text(files.now(), encoding="utf-8")
    for name in ("latest.json", "negative"):
        source = root / name
        if source.exists() and not (dest / name).exists():
            shutil.move(str(source), str(dest / name))
    (dest / _MOVED).write_text(files.now(), encoding="utf-8")


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


def _done_at(path: Path) -> datetime:
    try:
        return datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return datetime.max.replace(tzinfo=UTC)  # 읽을 수 없는 표지는 가장 나중으로 본다(거부)


def _other_restore_marker(settings, instance_id: str, request: dict[str, Any]) -> bool:
    """이 요청이 만들어진 뒤에 다른 복원이 게시를 시작했거나 끝냈는가
    (``restored/<다른 식별자>/.publishing``·``.done``)."""
    root = settings.export_dir / instance_id / "restored"
    created = request["created_at"].astimezone(UTC)
    mine = str(request["id"])
    if not root.is_dir():
        return False
    for name in (_PUBLISHING, _DONE):
        for marker in root.glob(f"*/{name}"):
            if marker.parent.name != mine and _done_at(marker) >= created:
                return True
    return False


def _bound_ok(settings, instance_id: str, request: dict[str, Any]) -> bool:
    """게시(재개 포함) 직전에 원래 위치의 현재 ``latest.json``만 본다(보관본은 보지 않는다).

    파일이 없으면(이 요청이 옮겼거나 처음부터 없던 경우) 허용하고, 있으면 요청에 결속한 해시와
    같을 때만 허용한다. 다른 복원이 게시를 시작했거나 끝냈다면 거부한다.
    """
    if _other_restore_marker(settings, instance_id, request):
        return False
    path = files.latest_path(settings.export_dir, instance_id)
    current = files.file_sha256(path)
    return current in ("absent", request["bound_latest"])


def _reject(job: JobContext, request: dict[str, Any]) -> None:
    conn, iid = job.conn, job.instance_id
    with conn.transaction():
        conn.execute(
            "UPDATE mint.restore_requests SET status = 'rejected'"
            " WHERE id = %s AND status = 'pending'", (request["id"],))
        alerts.raise_alert(
            conn, iid, kind="restore_request_rejected", severity="critical",
            dedup_key=f"restore_request_rejected:{request['id']}",
            subject={"request_id": str(request["id"]), "reason": "latest_changed"})
    raise PermanentFailure("restore_binding_mismatch")


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
        if not folder.is_dir():  # 아직 옮기기 전에 죽었다면 원래 자리에 있다
            folder = settings.export_dir / iid / "negative"
        names = [p for p in folder.glob("*.json") if p.stem.isdigit()] if folder.is_dir() else []
        path = max(names, key=lambda p: int(p.stem), default=None)
    else:
        path = files.newest_negative(settings.export_dir, iid)
    if path is None:
        return []
    return files.read_verified(path, keys).get("items") or []


def _original_latest(conn, iid: str, settings, request_id: str, archived: bool) -> dict | None:
    """복원 대상이었던 ``latest.json``(보관했다면 보관본). 읽지 못하면 None."""
    path = files.latest_path(settings.export_dir, iid)
    if archived and (_dest(settings, iid, request_id) / "latest.json").exists():
        path = _dest(settings, iid, request_id) / "latest.json"
    try:
        return files.read_verified(path, exports.keys(settings))
    except (FileNotFoundError, files.ExportUnverified):
        return None


def _anchor_ok(conn, iid: str, latest: dict | None) -> bool:
    """원본 ``latest.json``의 봉인 끝이 DB의 ``seal_batches``와 같은가. 읽지 못하면 거짓이다."""
    if latest is None:
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
            latest = _original_latest(conn, iid, settings, request_id, archived)
            items = _original_items(settings, iid, request_id, archived)
            bad = restore_service.compare(
                conn, iid, items, None if latest is None else latest.get("to_seal_seq"))
            # 같은 판본의 내용 차이(content)는 admin 2인이 인정한 경우만 허용한다
            ok = all(p["problem"] == "content" for p in bad)
            if ok and (bad or not _anchor_ok(conn, iid, latest)):
                # 요청의 cause 값을 믿지 않고 원본 latest.json 기준으로 다시 계산한 결과다
                # 앵커 불일치·내용 차이: admin 2인이고 사건의 두 번째 승인자가 요청과 같아야 한다
                ok = event["payload"].get("second_approver") == request["second_approver"] and (
                    _two_admins(conn, iid, request["requested_by"], request["second_approver"])
                )
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
    if request["status"] != "pending":
        return
    if (_dest(settings, iid, str(request["id"])) / _DONE).exists():
        # 이미 게시를 끝낸 요청이 DB 복구로 되살아났다: 파일은 건드리지 않고 상태만 맞춘다.
        conn.execute(
            "UPDATE mint.restore_requests SET status = 'published', published_at = now()"
            " WHERE id = %s AND status = 'pending'", (request["id"],))
        return
    lock = f"mint.restore_publish:{iid}"
    got = fetchone(conn, "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (lock,))["ok"]
    if not got:
        raise exports.ExportFailed("다른 실행기가 복원 파일을 게시하는 중입니다.")
    try:
        job.checkpoint()
        if request["bound_latest"] is None:  # 복원 명령이 만들지 않은 요청
            raise ControlError("복원 요청에 외부 기준(latest.json 해시) 결속이 없습니다.")
        if not _bound_ok(settings, iid, request):
            _reject(job, request)
        validate(job, request)
        archive(settings, iid, str(request["id"]))
        publishing = _dest(settings, iid, str(request["id"])) / _PUBLISHING
        if not publishing.exists():  # 다른 요청이 이 시점 뒤에 재개하지 못하게 먼저 남긴다
            publishing.write_text(_stamp(), encoding="utf-8")
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
        done = _dest(settings, iid, str(request["id"])) / _DONE
        done.write_text(_stamp(), encoding="utf-8")  # 완료 표지는 DB 확정보다 먼저 남긴다
        conn.execute(
            "UPDATE mint.restore_requests SET status = 'published', published_at = now()"
            " WHERE id = %s AND status = 'pending'", (request["id"],),
        )
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock,))
