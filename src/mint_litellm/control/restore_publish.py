"""작업 ``restore_publish``: 복원 요청의 파일을 게시한다(해제 경로).

되돌림 차단 상태에서도 이 작업만은 실행된다. 순서: 기존 파일을 ``restored/<요청>/``로 옮겨 보관 ->
DB 기준 새 부정 상태·체크포인트·``latest.json`` 게시(다시 읽어 HMAC·내용 검증) -> ``exports`` 행
확정 -> 요청을 ``published``로. 중간에 죽거나 임대를 잃으면 차단이 유지되고(``latest.json``이 없거나
옛 값이다) 다시 실행하면 이어서 끝낸다. 같은 인스턴스의 게시는 자문 잠금으로 한 번에 하나다.
"""

import shutil

from mint_litellm.control import exports
from mint_litellm.control.db.connection import fetchone
from mint_litellm.control.errors import ControlError
from mint_litellm.control.jobs import JobContext
from mint_litellm.restore import files, judge

_MARKER = ".archived"


def archive(settings, instance_id: str, request_id: str) -> None:
    """기존 ``latest.json``과 ``negative/``를 지우지 않고 ``restored/<요청>/``로 옮긴다."""
    root = settings.export_dir / instance_id
    dest = root / "restored" / request_id
    if (dest / _MARKER).exists():
        return  # 이미 보관했다(재실행)
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("latest.json", "negative"):
        source = root / name
        if source.exists():
            shutil.move(str(source), str(dest / name))
    (dest / _MARKER).write_text(files.now(), encoding="utf-8")


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
        archive(settings, iid, str(request["id"]))
        job.checkpoint()
        exports.write_export(
            conn, iid, settings, "negative_state", force_latest=True, republish=True,
            actor="worker",
        )
        job.checkpoint()
        exports.write_export(
            conn, iid, settings, "checkpoint", force_latest=True, actor="worker"
        )
        state = judge.read_files(settings.export_dir, iid, exports.keys(settings))
        if state.error is not None or state.latest is None:
            raise exports.ExportFailed("게시한 파일을 다시 읽어 검증하지 못했습니다.")
        job.checkpoint()
        conn.execute(
            "UPDATE mint.restore_requests SET status = 'published', published_at = now()"
            " WHERE id = %s AND status = 'pending'", (request["id"],),
        )
    finally:
        conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock,))
