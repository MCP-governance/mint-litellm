"""수용검증 작업 처리기: ``submission_poll``, ``intake_run``, ``evidence_expiry_scan``.

LiteLLM 읽기·쓰기는 모두 제어 평면 클라이언트(``JobContext.litellm``)를 통한다. 쓰기(제출
승인·거절)는 클라이언트의 쓰기 메서드 한 곳을 지나며 그 직전에 ``guard_external_write``가 불린다.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from mint_litellm.control import intake_checks as checks
from mint_litellm.control.context import Context, Operator
from mint_litellm.control.db.connection import fetchall
from mint_litellm.control.errors import ControlError
from mint_litellm.control.jobs import JobContext, guard_external_write
from mint_litellm.control.services import intakes
from mint_litellm.control.services.base import enqueue_job
from mint_litellm.types.ids import new_uuid7
from mint_litellm.types.jobs import intake_run_job_key

POLL_LIMIT = 500
EXPIRY_WARNING = timedelta(days=7)


class LiteLLMNotConfigured(ControlError):
    code = "litellm_not_configured"


def _client(job: JobContext) -> Any:
    if job.litellm is None:
        raise LiteLLMNotConfigured("MINT_LITELLM_URL과 MINT_LITELLM_KEY가 필요합니다.")
    return job.litellm.with_guard(lambda: guard_external_write(job))


def _context(job: JobContext) -> Context:
    return Context(job.conn, job.instance_id, Operator(intakes.SYSTEM_ACTOR, "admin", None))


# --- submission_poll ---------------------------------------------------------------------


def submission_poll(job: JobContext) -> None:
    """LiteLLM 제출(``pending_review``)을 읽어 신청(``received``)을 만든다. 한 번에 500건까지."""
    client = _client(job)
    job.checkpoint()
    pending = [i for i in client.list_submissions() if i.get("approval_status") == "pending_review"]
    ctx = _context(job)
    for item in pending[:POLL_LIMIT]:
        job.checkpoint()
        intakes.receive_submission(ctx, item)


# --- intake_run ---------------------------------------------------------------------------


def _allowlists(job: JobContext, org_id: str) -> list[dict]:
    return fetchall(
        job.conn,
        "SELECT id, kind, value FROM mint.allowlists WHERE instance_id = %s AND status = 'active'"
        " AND valid_from <= now() AND valid_until > now() AND (org_id IS NULL OR org_id = %s)",
        (job.instance_id, org_id),
    )


def _stored_definitions(job: JobContext, server_id: str) -> list[dict] | None:
    rows = fetchall(
        job.conn,
        "SELECT canonical_text FROM mint.tool_contracts WHERE instance_id = %s AND server_id = %s"
        " AND status = 'candidate' AND source = 'intake' ORDER BY tool",
        (job.instance_id, server_id),
    )
    return [json.loads(r["canonical_text"]) for r in rows] or None


def _registry_first(client: Any, server_id: str) -> str | None:
    """제출을 LiteLLM 레지스트리에 올린다(``allow_all_keys=false``인 제출만). 막히면 사유 코드."""
    item = next((i for i in client.list_submissions() if i.get("server_id") == server_id), None)
    if item is None:
        return "SUBMISSION_GONE"
    if item.get("allow_all_keys"):
        # approve는 제출의 allow_all_keys를 그대로 둔다. 모든 키에 열린 서버는 올리지 않는다.
        return "ALL_KEYS_OPEN"
    if item.get("approval_status") == "pending_review":
        client.approve_submission(server_id)
    return None


def _run_checks(
    job: JobContext, ctx: Context, row: dict, attempt: int, mode: str, persist: bool
) -> bool:
    """검사를 순서대로 실행·기록한다. 앞 단계가 실패하면 멈춘다. 자격 필요 여부를 돌려준다."""
    client = _client(job)
    server_id = row["server_id"]
    contract = intakes.deployment_candidate(job.conn, job.instance_id, server_id)
    allowlists = _allowlists(job, row["org_id"])
    data = checks.load_policy_data(job.policy_root)
    ports = tuple(getattr(job.settings, "intake_allowed_ports", checks.DEFAULT_PORTS))
    defs: list[dict] | None = None
    needs_credentials = False
    spec = None
    if contract is not None:
        spec = {"url": contract["url"], "transport": contract["transport"],
                "auth_type": contract["auth_type"]}
    for name in checks.CHECKS:
        job.checkpoint()
        done = intakes.existing_check(job.conn, job.instance_id, row["id"], name, attempt)
        if done is not None:  # 재시도: 이 시도에서 이미 기록한 검사는 건너뛴다
            if name == "connection":
                needs_credentials = done["reason_code"] == "CREDENTIALS_REQUIRED"
            if name == "definitions" and done["result"] == "pass":
                defs = _stored_definitions(job, server_id)
            if done["result"] == "fail":
                return needs_credentials
            continue
        if name == "url_guard":
            out = checks.url_guard(contract and contract["url"], contract and contract["transport"],
                                   allowlists, ports)
        elif name == "oauth_issuer_allowlist":
            out = checks.oauth_issuer_allowlist(
                contract and contract["auth_type"], contract and contract["issuer"], allowlists)
        elif name in ("connection", "definitions"):
            probe = dict(spec or {})
            blocked = None
            if mode == "registry":
                blocked = _registry_first(client, server_id) if name == "connection" else None
                probe["server_id"] = server_id
            if blocked:
                out = checks.Outcome("indeterminate", blocked, "litellm:registry_first")
            elif name == "connection":
                out = checks.connection(client, probe)
            else:
                out = checks.definitions(client, probe)
                defs = out.extra.get("definitions")
                if persist and defs is not None:
                    intakes.store_candidates(ctx, server_id, defs)
            if name == "connection":
                needs_credentials = out.reason == "CREDENTIALS_REQUIRED"
        elif name == "description_scan":
            out = checks.description_scan(defs, data)
        elif name == "risk_grade":
            out = checks.risk_grade(defs, row["data_grade"], data)
            graded = out.extra.get("graded")
            if persist and graded:
                intakes.store_grades(ctx, server_id, graded)
        elif name == "termination_readiness":
            out = checks.termination_readiness(row["readiness"])
        else:
            out = checks.supply_chain(contract and contract["image_digest"],
                                      contract and contract["source_commit"])
        intakes.record_check(ctx, row, attempt, name, out)
        if out.result == "fail":
            break
    return needs_credentials


def _reject_at_litellm(job: JobContext, row: dict) -> None:
    """결정된 거절을 LiteLLM 제출에 한 번 전달한다(이미 거절됐으면 그대로 끝)."""
    if row["source"] != "submission" or row["decision"] != "rejected":
        return
    reason = (row["decision_reason"] or "rejected")[:200]
    _client(job).reject_submission(row["server_id"], reason)


def intake_run(job: JobContext) -> None:
    target, _, mode = job.job["target_ref"].partition("/")
    attempt = int(job.job["idempotency_key"].rsplit(":", 1)[1])
    ctx = _context(job)
    row = intakes.get(ctx, target)
    if mode == "reject":
        _reject_at_litellm(job, row)
        return
    if mode == "evidence":
        _run_checks(job, ctx, row, attempt, mode, persist=False)
        return
    if row["status"] == "decided":
        _reject_at_litellm(job, row)  # 자동 거절 뒤 LiteLLM 거절이 끊긴 경우 이어서 한다
        return
    started = intakes.begin_checking(ctx, target)
    if started is None:
        return  # 필수 입력이 없으면 검사로 가지 않는다
    needs = _run_checks(job, ctx, started, attempt, mode, persist=True)
    verdict = intakes.conclude(ctx, target, needs_registry_credentials=needs)
    if verdict == "failed":
        _reject_at_litellm(job, intakes.get(ctx, target))


# --- evidence_expiry_scan -------------------------------------------------------------------


def _open_alert(job: JobContext, kind: str, dedup: str, subject: dict) -> None:
    job.conn.execute(
        "INSERT INTO mint.alerts (id, instance_id, kind, severity, dedup_key, subject)"
        " VALUES (%s, %s, %s, 'medium', %s, %s)"
        " ON CONFLICT (instance_id, dedup_key) WHERE status <> 'resolved'"
        " DO UPDATE SET count = mint.alerts.count + 1, last_at = now()",
        (new_uuid7(), job.instance_id, kind, dedup[:512], Jsonb(subject)),
    )


def evidence_expiry_scan(job: JobContext) -> None:
    """승인된 신청의 증거가 7일 안에 만료되면 경보 ``evidence_expiring``과 재검사를 등록한다."""
    ctx = _context(job)
    rows = fetchall(
        job.conn,
        "SELECT DISTINCT ON (i.server_id) i.id, i.server_id FROM mint.intakes i"
        " JOIN mint.servers s ON s.instance_id = i.instance_id AND s.server_id = i.server_id"
        " WHERE i.instance_id = %s AND i.status = 'decided'"
        " AND i.decision IN ('approved', 'exception') AND s.contract_status = 'managed'"
        " AND s.status NOT IN ('rejected', 'terminating', 'terminated')"
        " ORDER BY i.server_id, i.decided_at DESC",
        (job.instance_id,),
    )
    horizon = datetime.now(UTC) + EXPIRY_WARNING
    for row in rows:
        job.checkpoint()
        expiring = [
            c for c in intakes.latest_checks(job.conn, job.instance_id, row["id"])
            if c["evidence_valid_until"] is not None and c["evidence_valid_until"] <= horizon
        ]
        if not expiring:
            continue
        for c in expiring:
            _open_alert(job, "evidence_expiring", f"evidence_expiring:{row['id']}:{c['check']}",
                        {"intake_id": str(row["id"]), "server_id": row["server_id"],
                         "check": c["check"],
                         "valid_until": c["evidence_valid_until"].isoformat()})
        attempt = intakes.next_attempt(job.conn, job.instance_id, row["id"])
        enqueue_job(ctx, "intake_run", f"{row['id']}/evidence",
                    intake_run_job_key(row["id"], attempt))


HANDLERS = {
    "submission_poll": submission_poll,
    "intake_run": intake_run,
    "evidence_expiry_scan": evidence_expiry_scan,
}
