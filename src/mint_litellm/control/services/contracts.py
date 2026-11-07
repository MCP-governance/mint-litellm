"""계약 비교와 증거·직무 분리·재pin을 확인한 재승인."""

import json
from datetime import timedelta
from uuid import UUID

from mint_litellm.control.context import Context
from mint_litellm.control.db.connection import fetchall, fetchone
from mint_litellm.control.errors import (
    conflict,
    not_found,
    separation_of_duties,
    stale_evidence,
)
from mint_litellm.control.services.base import require_role, service, transact
from mint_litellm.types.canonical import (
    canonical_text,
    credential_scope,
    pin_set_sha256,
    pin_sha256,
    sha256,
)
from mint_litellm.types.contracts import ActionType, DataGrade, RiskGrade, risk_grade
from mint_litellm.types.decision import url_sha256
from mint_litellm.types.events import EventKind, control_source_key
from mint_litellm.types.ids import new_uuid7


@service("contract show")
def show(ctx: Context, server_id: str) -> dict:
    server = fetchone(
        ctx.conn,
        "SELECT server_id,contract_status,state_version,last_observed_at,"
        " deployment_drift,deployment_drift_fields FROM mint.servers"
        " WHERE instance_id=%s AND server_id=%s",
        (ctx.instance_id, server_id),
    )
    if server is None:
        raise not_found("서버가 없습니다.")
    return {
        "server": server,
        "tools": fetchall(
            ctx.conn,
            "SELECT * FROM mint.tool_contracts WHERE instance_id=%s AND server_id=%s"
            " ORDER BY credential_scope,tool,version",
            (ctx.instance_id, server_id),
        ),
        "deployments": fetchall(
            ctx.conn,
            "SELECT * FROM mint.deployment_contracts WHERE instance_id=%s AND server_id=%s"
            " ORDER BY version",
            (ctx.instance_id, server_id),
        ),
    }


@service("contract diff")
def diff(ctx: Context, server_id: str) -> dict:
    current = show(ctx, server_id)
    current["observations"] = fetchall(
        ctx.conn,
        "SELECT kind,scope,name,observed_sha256,canonical_text,change_seq,observed_at"
        " FROM mint.contract_observations WHERE instance_id=%s AND server_id=%s"
        " ORDER BY kind,scope,name",
        (ctx.instance_id, server_id),
    )
    return current


@service("contract reapprove")
def request_deployment(
    ctx: Context, server_id: str, candidate_id: str, *, expected_version: int | None = None
) -> None:
    """수용검증이 만든 배포 후보를 재승인 대기 상태로 기록한다."""
    from mint_litellm.control.contract_observe import _alert, _observe

    server = fetchone(
        ctx.conn,
        "SELECT * FROM mint.servers WHERE instance_id=%s AND server_id=%s FOR UPDATE",
        (ctx.instance_id, server_id),
    )
    if server is None:
        raise not_found("서버가 없습니다.")
    if server["contract_status"] != "managed":
        raise conflict("관리 서버의 배포 후보만 갱신할 수 있습니다.")
    if expected_version is not None and server["state_version"] != expected_version:
        raise conflict("서버 상태 버전이 다릅니다.")
    contracts = fetchall(
        ctx.conn,
        "SELECT * FROM mint.deployment_contracts WHERE instance_id=%s AND server_id=%s FOR UPDATE",
        (ctx.instance_id, server_id),
    )
    old = next((c for c in contracts if c["status"] == "approved"), None)
    candidate = next(
        (c for c in contracts if str(c["id"]) == candidate_id and c["status"] == "candidate"), None
    )
    if old is None or candidate is None:
        raise conflict("승인본과 새 배포 후보가 필요합니다.")
    fields = [
        key
        for key in (
            "url_sha256",
            "transport",
            "auth_type",
            "issuer",
            "oauth_scopes",
            "allowed_tools",
            "image_digest",
            "source_commit",
        )
        if old[key] != candidate[key]
    ]
    if not fields:
        return
    observation, changed = _observe(
        ctx,
        server_id,
        "deployment",
        "server",
        "identity",
        old["id"],
        {"candidate_sha256": candidate["sha256"]},
    )
    merged = sorted(set(server["deployment_drift_fields"] or ()) | set(fields))
    if not server["deployment_drift"] or merged != server["deployment_drift_fields"]:
        ctx.conn.execute(
            "UPDATE mint.servers SET deployment_drift=true,deployment_drift_fields=%s"
            " WHERE instance_id=%s AND server_id=%s",
            (merged, ctx.instance_id, server_id),
        )
    if changed:
        payload = {
            "server_id": server_id,
            "fields": fields,
            "version": old["version"],
            "candidate_id": candidate_id,
            "kind": "deployment",
        }
        ctx.emit(EventKind.DEPLOYMENT_DRIFT_DETECTED, old["id"], observation["change_seq"], payload)
        ctx.emit(EventKind.REAPPROVAL_REQUESTED, old["id"], observation["change_seq"], payload)
        _alert(ctx, old["id"], observation["observed_sha256"], "high", payload)


def _plan(
    ctx: Context,
    server_id: str,
    intake_id: str,
    candidate_ids: list[str],
    deployment_id: str | None,
    expected_version: int | None,
) -> dict:
    try:
        for value in [intake_id, *candidate_ids, *([deployment_id] if deployment_id else [])]:
            UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise conflict("신청과 후보 계약 식별자는 UUID여야 합니다.") from None
    server = fetchone(
        ctx.conn,
        "SELECT *,now() AS checked_at FROM mint.servers"
        " WHERE instance_id=%s AND server_id=%s FOR UPDATE",
        (ctx.instance_id, server_id),
    )
    if server is None:
        raise not_found("서버가 없습니다.")
    if server["contract_status"] != "managed" or server["status"] != "approved":
        raise conflict("승인된 관리 서버만 재승인할 수 있습니다.")
    if expected_version is not None and server["state_version"] != expected_version:
        raise conflict("서버 상태 버전이 다릅니다.")
    intake = fetchone(
        ctx.conn,
        "SELECT * FROM mint.intakes WHERE instance_id=%s AND id=%s AND server_id=%s FOR UPDATE",
        (ctx.instance_id, intake_id, server_id),
    )
    if intake is None or intake["status"] not in {"passed", "decided"}:
        raise stale_evidence("통과한 재검사 신청이 필요합니다.")
    if intake["status"] == "decided" and intake["decision"] != "approved":
        raise stale_evidence("승인되지 않은 신청의 증거입니다.")
    if not ctx.operator.user_id or not intake["requested_by"]:
        raise separation_of_duties("신청자와 승인자의 사용자 결속이 필요합니다.")
    if ctx.operator.user_id == intake["requested_by"]:
        raise separation_of_duties("신청자는 같은 계약을 재승인할 수 없습니다.")
    if not candidate_ids and deployment_id is None:
        raise conflict("새 후보 계약을 지정하십시오.")
    if len(set(candidate_ids)) != len(candidate_ids):
        raise conflict("중복된 후보 계약입니다.")
    candidates = (
        fetchall(
            ctx.conn,
            "SELECT * FROM mint.tool_contracts WHERE instance_id=%s"
            " AND server_id=%s AND id=ANY(%s::uuid[]) FOR UPDATE",
            (ctx.instance_id, server_id, candidate_ids),
        )
        if candidate_ids
        else []
    )
    if len(candidates) != len(candidate_ids) or any(c["status"] != "candidate" for c in candidates):
        raise conflict("후보 도구 계약이 없거나 이미 처리되었습니다.")
    deployment = (
        fetchone(
            ctx.conn,
            "SELECT * FROM mint.deployment_contracts"
            " WHERE instance_id=%s AND server_id=%s AND id=%s AND status='candidate' FOR UPDATE",
            (ctx.instance_id, server_id, deployment_id),
        )
        if deployment_id
        else None
    )
    if deployment_id and deployment is None:
        raise conflict("후보 배포 계약이 없습니다.")
    auth = deployment["auth_type"] if deployment else server["auth_type"]
    scope = credential_scope(auth)
    if any(c["credential_scope"] != scope for c in candidates):
        raise conflict("배포 계약의 자격 범위와 후보 도구 계약이 다릅니다.")
    if len({c["tool"] for c in candidates}) != len(candidates):
        raise conflict("한 도구에 여러 후보를 지정할 수 없습니다.")
    approved = fetchall(
        ctx.conn,
        "SELECT * FROM mint.tool_contracts WHERE instance_id=%s"
        " AND server_id=%s AND credential_scope=%s AND status='approved' FOR UPDATE",
        (ctx.instance_id, server_id, scope),
    )
    desired = {c["tool"]: json.loads(c["canonical_text"]) for c in approved}
    for candidate in candidates:
        value = json.loads(candidate["canonical_text"])
        if (
            canonical_text(value) != candidate["canonical_text"]
            or sha256(value) != candidate["sha256"]
            or pin_sha256(value) != candidate["pin_sha256"]
        ):
            raise conflict("후보 계약의 해시가 일치하지 않습니다.")
        try:
            grade = risk_grade(
                DataGrade(intake["data_grade"]),
                ActionType(candidate["action_type"]),
            )
            declared = RiskGrade(candidate["risk_grade"])
        except (ValueError, TypeError):
            raise stale_evidence("후보 도구의 위험 등급 재검사가 필요합니다.") from None
        # 비가역성의 상향 평가는 후보에 결속된 수용검증 증거가 담당한다.
        if tuple(RiskGrade).index(declared) < tuple(RiskGrade).index(grade):
            raise stale_evidence("후보 도구의 위험 등급이 재산정 하한보다 낮습니다.")
        desired[candidate["tool"]] = value
    if deployment and sha256(json.loads(deployment["canonical_text"])) != deployment["sha256"]:
        raise conflict("배포 계약의 해시가 일치하지 않습니다.")
    if deployment:
        definition = json.loads(deployment["canonical_text"])
        if url_sha256(deployment["url"]) != deployment["url_sha256"] or any(
            definition.get(key) != deployment[key]
            for key in (
                "url",
                "transport",
                "auth_type",
                "issuer",
                "oauth_scopes",
                "allowed_tools",
                "image_digest",
                "source_commit",
            )
        ):
            raise conflict("배포 계약 전문과 배포 값이 다릅니다.")
    evidence_hash = sha256(
        {
            "tools": {c["tool"]: c["sha256"] for c in candidates},
            "deployment": deployment["sha256"] if deployment else None,
        }
    )
    checks = fetchall(
        ctx.conn,
        'SELECT DISTINCT ON ("check") * FROM mint.intake_checks'
        ' WHERE instance_id=%s AND intake_id=%s ORDER BY "check",attempt DESC',
        (ctx.instance_id, intake_id),
    )
    by_name = {row["check"]: row for row in checks}
    required = {"connection", "definitions", "description_scan", "risk_grade"}
    if deployment:
        required |= {"url_guard", "connection", "oauth_issuer_allowlist"}
    for name in required:
        row = by_name.get(name)
        if (
            row is None
            or row["result"] != "pass"
            or not server["checked_at"] - timedelta(hours=24)
            <= row["ran_at"]
            <= server["checked_at"]
            or row["inputs"].get("contract_sha256") != evidence_hash
            or not row["evidence_sha256"]
            or row["evidence_valid_until"] is None
            or row["evidence_valid_until"] <= server["checked_at"]
        ):
            raise stale_evidence("후보에 결속된 24시간 이내 재검사 증거가 필요합니다.")
    return {
        "server": server,
        "candidates": candidates,
        "deployment": deployment,
        "approved": approved,
        "desired": desired,
        "scope": scope,
        "evidence_hash": evidence_hash,
    }


def verify_pin(desired: dict, response: dict) -> str:
    if not response or set(response) != set(desired):
        raise conflict("재pin 도구 집합이 후보 계약과 다릅니다.")
    try:
        if any(pin_sha256(response[name]) != pin_sha256(value) for name, value in desired.items()):
            raise conflict("재pin 해시가 후보 계약과 다릅니다.")
        return pin_set_sha256(response)
    except (ValueError, TypeError, KeyError):
        raise conflict("재pin 응답의 정의 형식이 올바르지 않습니다.") from None


def _reapprove(
    ctx: Context,
    pin_client,
    server_id: str,
    intake_id: str,
    candidate_ids: list[str],
    *,
    deployment_id: str | None = None,
    expected_version: int | None = None,
) -> dict:
    require_role(ctx, "contract reapprove")
    # 외부 쓰기는 열린 DB 트랜잭션을 받지 않는다.
    from psycopg.pq import TransactionStatus

    if ctx.conn.info.transaction_status != TransactionStatus.IDLE:
        raise conflict("재승인은 유휴 연결에서 실행해야 합니다.")
    with transact(ctx):
        plan = _plan(ctx, server_id, intake_id, candidate_ids, deployment_id, expected_version)
        # 외부 pin 중 다른 승인 호출이 새 정의를 실행하지 못하도록 먼저 닫는다.
        fields = set(plan["server"]["deployment_drift_fields"] or ())
        if plan["server"]["deployment_drift"] and not fields:
            fields.add("unknown")
        fields.add("pin_reapproval")
        marker = fetchone(
            ctx.conn,
            "UPDATE mint.servers SET deployment_drift=true,deployment_drift_fields=%s"
            " WHERE instance_id=%s AND server_id=%s RETURNING state_version",
            (sorted(fields), ctx.instance_id, server_id),
        )["state_version"]
        requested = [(c, "tool") for c in plan["candidates"]]
        if plan["deployment"]:
            requested.append((plan["deployment"], "deployment"))
        for candidate, kind in requested:
            key = control_source_key(
                EventKind.REAPPROVAL_REQUESTED, str(candidate["id"]), candidate["version"]
            )
            if (
                fetchone(
                    ctx.conn,
                    "SELECT 1 FROM mint.events WHERE instance_id=%s AND source_key=%s",
                    (ctx.instance_id, key),
                )
                is None
            ):
                ctx.emit(
                    EventKind.REAPPROVAL_REQUESTED,
                    candidate["id"],
                    candidate["version"],
                    {
                        "server_id": server_id,
                        "version": candidate["version"],
                        "kind": kind,
                        "candidate_sha256": candidate["sha256"],
                    },
                )
    try:
        response = pin_client.pin(server_id)
        digest = verify_pin(plan["desired"], response)
    except Exception:
        pin_client.unpin(server_id)
        raise
    with transact(ctx):
        fresh = _plan(ctx, server_id, intake_id, candidate_ids, deployment_id, marker)
        if fresh["evidence_hash"] != plan["evidence_hash"] or fresh["desired"] != plan["desired"]:
            raise conflict("재pin 중 후보 또는 증거가 변경되었습니다.")
        for candidate in fresh["candidates"]:
            old = next((c for c in fresh["approved"] if c["tool"] == candidate["tool"]), None)
            if old:
                row = fetchone(
                    ctx.conn,
                    "UPDATE mint.tool_contracts SET status='superseded'"
                    " WHERE instance_id=%s AND id=%s RETURNING version",
                    (ctx.instance_id, old["id"]),
                )
                ctx.emit(
                    EventKind.CONTRACT_SUPERSEDED,
                    old["id"],
                    row["version"],
                    {"server_id": server_id, "replacement": str(candidate["id"])},
                )
            row = fetchone(
                ctx.conn,
                "UPDATE mint.tool_contracts SET status='approved',drift='none',"
                " observed_sha256=sha256,observed_at=now() WHERE instance_id=%s AND id=%s"
                " RETURNING version",
                (ctx.instance_id, candidate["id"]),
            )
            ctx.emit(
                EventKind.CONTRACT_APPROVED,
                candidate["id"],
                row["version"],
                {"server_id": server_id, "pin_sha256": candidate["pin_sha256"]},
            )
        deployment = fresh["deployment"]
        if deployment:
            old = fetchone(
                ctx.conn,
                "UPDATE mint.deployment_contracts SET status='superseded'"
                " WHERE instance_id=%s AND server_id=%s AND status='approved' RETURNING id,version",
                (ctx.instance_id, server_id),
            )
            if old:
                ctx.emit(
                    EventKind.DEPLOYMENT_CONTRACT_SUPERSEDED,
                    old["id"],
                    old["version"],
                    {"server_id": server_id},
                )
            row = fetchone(
                ctx.conn,
                "UPDATE mint.deployment_contracts SET status='approved'"
                " WHERE instance_id=%s AND id=%s RETURNING version",
                (ctx.instance_id, deployment["id"]),
            )
            ctx.emit(
                EventKind.DEPLOYMENT_CONTRACT_APPROVED,
                deployment["id"],
                row["version"],
                {"server_id": server_id},
            )
            ctx.conn.execute(
                "UPDATE mint.servers SET url_sha256=%s,transport=%s,auth_type=%s,issuer=%s"
                " WHERE instance_id=%s AND server_id=%s",
                (
                    deployment["url_sha256"],
                    deployment["transport"],
                    deployment["auth_type"],
                    deployment["issuer"],
                    ctx.instance_id,
                    server_id,
                ),
            )
        # 배포 재검사 없는 도구 재승인으로 기존 배포 드리프트를 지우지 않는다.
        clear = deployment is not None or fields == {"pin_reapproval"}
        ctx.conn.execute(
            "UPDATE mint.servers SET last_observed_at=now(),deployment_drift=%s,"
            " deployment_drift_fields=CASE WHEN %s THEN NULL ELSE deployment_drift_fields END"
            " WHERE instance_id=%s AND server_id=%s",
            (not clear, clear, ctx.instance_id, server_id),
        )
        ctx.conn.execute(
            "INSERT INTO mint.litellm_grants"
            " (id,instance_id,target_type,target_id,item,server_id,preexisting,applied_sha256,"
            " observed,observed_at,last_applied_at)"
            " VALUES (%s,%s,'server',%s,'pin',%s,false,%s,true,now(),now())"
            " ON CONFLICT (instance_id,target_type,target_id,item,server_id) DO UPDATE"
            " SET applied_sha256=EXCLUDED.applied_sha256,observed=true,observed_at=now(),"
            " last_applied_at=now()",
            (new_uuid7(), ctx.instance_id, server_id, server_id, digest),
        )
        return {
            "server_id": server_id,
            "pin_sha256": digest,
            "contracts": [str(c["id"]) for c in fresh["candidates"]],
        }


def reapprove(
    ctx: Context,
    pin_client,
    server_id: str,
    intake_id: str,
    candidate_ids: list[str],
    *,
    deployment_id: str | None = None,
    expected_version: int | None = None,
) -> dict:
    from psycopg.pq import TransactionStatus

    require_role(ctx, "contract reapprove")
    if ctx.conn.info.transaction_status != TransactionStatus.IDLE:
        raise conflict("재승인은 유휴 연결에서 실행해야 합니다.")
    key = f"contract-pin:{ctx.instance_id}:{server_id}"
    # 세션 잠금은 외부 호출 동안에도 유지되지만 DB 트랜잭션은 열어 두지 않는다.
    with ctx.conn.transaction():
        locked = fetchone(
            ctx.conn, "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS ok", (key,)
        )["ok"]
    if not locked:
        raise conflict("이 서버의 재승인이 진행 중입니다.")
    try:
        return _reapprove(
            ctx,
            pin_client,
            server_id,
            intake_id,
            candidate_ids,
            deployment_id=deployment_id,
            expected_version=expected_version,
        )
    finally:
        with ctx.conn.transaction():
            ctx.conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))
