"""화면(서버 렌더링). 변경은 서비스 함수를 부르고, 실패하면 같은 화면에 오류를 보인다."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse, Response

from mint_litellm.console import api
from mint_litellm.console.core import STATUS, Actor, page_actor, require_csrf
from mint_litellm.console.templating import render
from mint_litellm.control import status as control_status
from mint_litellm.control.errors import AdminCode, AdminError, ControlError
from mint_litellm.control.services import allowlists as allowlist_service
from mint_litellm.control.services import approvals as approval_service
from mint_litellm.control.services import call_approvals as call_approval_service
from mint_litellm.control.services import denials as denial_service
from mint_litellm.control.services import orgs as org_service
from mint_litellm.control.services import reads
from mint_litellm.control.services import servers as server_service
from mint_litellm.control.services import settings as settings_service
from mint_litellm.types.settings import Mode

router = APIRouter(dependencies=[Depends(require_csrf)])

Rebuild = Callable[..., Response]


def page_number(request: Request) -> int:
    value = request.query_params.get("page", "1")
    return int(value) if value.isdigit() else 1


def lines(text: str) -> list[str]:
    return [part.strip() for part in text.replace(",", "\n").splitlines() if part.strip()]


def act(
    actor: Actor, action: Callable[[], Any], ok: str, back: str, rebuild: Rebuild
) -> Response:
    """서비스 호출 하나. 성공하면 알림과 함께 목록으로, 실패하면 같은 화면에 오류를 보인다."""
    try:
        action()
    except ControlError as error:
        actor.conn.rollback()
        status = STATUS[error.code] if isinstance(error, AdminError) else 400
        return rebuild(error=str(error), status=status)
    return RedirectResponse(f"{back}?ok={ok}", status_code=303)


# --- 개요 -----------------------------------------------------------------------------------


@router.get("/")
def overview(request: Request, actor: Actor = Depends(page_actor)):
    data = reads.overview(actor.conn, actor.instance)
    data["workers"] = control_status.enforcement_states(actor.ctx)
    chart = {
        "days": [d["day"] for d in data["decisions_by_day"]],
        "allowed": [d["allowed"] for d in data["decisions_by_day"]],
        "blocked": [d["blocked"] for d in data["decisions_by_day"]],
    }
    return render(request, "overview.html", {"o": data, "chart": chart}, actor=actor)


# --- MCP 서버 -------------------------------------------------------------------------------


@router.get("/servers")
def servers(request: Request, actor: Actor = Depends(page_actor)):
    return render(
        request, "servers.html", {"rows": reads.servers(actor.conn, actor.instance)}, actor=actor
    )


def server_page(request: Request, actor: Actor, server_id: str, error=None, status=200):
    row = reads.server(actor.conn, actor.instance, server_id)
    if row is None:
        raise AdminError(AdminCode.NOT_FOUND, f"서버 {server_id}이(가) 없습니다.")
    return render(
        request, "server_detail.html", {"s": row, "error": error}, actor=actor, status=status
    )


@router.get("/servers/{server_id}")
def server_detail(server_id: str, request: Request, actor: Actor = Depends(page_actor)):
    return server_page(request, actor, server_id)


@router.post("/servers/{server_id}/suspend")
def suspend_server(
    server_id: str,
    request: Request,
    state_version: int = Form(...),
    confirm_name: str = Form(""),
    actor: Actor = Depends(page_actor),
):
    def action():
        api.confirm(server_id, confirm_name, "서버")
        server_service.suspend(actor.ctx, server_id, expected_version=state_version)

    return act(actor, action, "suspended", f"/servers/{server_id}",
               lambda **kw: server_page(request, actor, server_id, **kw))


@router.post("/servers/{server_id}/resume")
def resume_server(
    server_id: str,
    request: Request,
    state_version: int = Form(...),
    actor: Actor = Depends(page_actor),
):
    return act(
        actor,
        lambda: server_service.resume(actor.ctx, server_id, expected_version=state_version),
        "resumed", f"/servers/{server_id}",
        lambda **kw: server_page(request, actor, server_id, **kw),
    )


# --- 승인 대기열·상세 -------------------------------------------------------------------------


def approvals_page(request: Request, actor: Actor, error=None, status=200, form=None):
    ctx = {
        "queue": reads.queue(actor.conn, actor.instance),
        "rows": reads.approvals(actor.conn, actor.instance, page=page_number(request)),
        "form": form,
        "page": page_number(request),
        "error": error,
        "can_grant": actor.config.litellm_admin is not None,
    }
    return render(request, "approvals.html", ctx, actor=actor, status=status)


@router.get("/approvals")
def approvals(request: Request, actor: Actor = Depends(page_actor)):
    return approvals_page(request, actor)


def approval_form_page(
    request: Request, actor: Actor, relation_id: str, error=None, status=200, days=None
):
    relation = reads.relation(actor.conn, actor.instance, relation_id)
    if relation is None:
        raise AdminError(AdminCode.NOT_FOUND, "이용 관계가 없습니다.")
    high = reads.high_risk_tools(actor.conn, actor.instance, relation["server_id"])
    form = {
        "relation": relation,
        "days": days or api.approval_days("standard", [], high),
        "strict_days": api.STRICT_DAYS,
        "max_exception_days": api.EXCEPTION_MAX_DAYS,
        "high": high,
        "can_grant": actor.config.litellm_admin is not None,
        "error": error,
    }
    return render(request, "approval_new.html", form, actor=actor, status=status)


@router.get("/approvals/new")
def approval_form(
    request: Request, relation_id: str = Query(...), actor: Actor = Depends(page_actor)
):
    return approval_form_page(request, actor, relation_id)


@router.post("/approvals")
def grant_approval(
    request: Request,
    relation_id: str = Form(...),
    state_version: int = Form(...),
    scope_type: str = Form(...),
    scope_id: str = Form(...),
    tools: str = Form(...),
    approval_class: str = Form("standard", alias="class"),
    basis: str = Form(""),
    requested_by: str = Form(...),
    valid_days: int | None = Form(None),
    second_approver_token: str = Form(""),
    actor: Actor = Depends(page_actor),
):
    def action():
        body = api.ApprovalBody(
            state_version=state_version, relation_id=relation_id, scope_type=scope_type,
            scope_id=scope_id, tools=lines(tools), approval_class=approval_class,
            basis=basis.strip() or f"콘솔 승인(신청자 {requested_by})", requested_by=requested_by,
            valid_days=valid_days, second_approver_token=second_approver_token or None,
        )
        api.grant_approval(body, actor)

    def rebuild(**kw):
        return approval_form_page(request, actor, relation_id, days=valid_days, **kw)

    return act(actor, action, "approved", "/approvals", rebuild)


# --- 거부·철회 ------------------------------------------------------------------------------


def revocations_page(request: Request, actor: Actor, error=None, status=200):
    ctx = {
        "approvals": reads.approvals(actor.conn, actor.instance, status="active"),
        "denials": reads.denials(actor.conn, actor.instance),
        "error": error,
    }
    return render(request, "revocations.html", ctx, actor=actor, status=status)


@router.get("/revocations")
def revocations(request: Request, actor: Actor = Depends(page_actor)):
    return revocations_page(request, actor)


@router.post("/approvals/{approval_id}/revoke")
def revoke_approval(
    approval_id: str,
    request: Request,
    state_version: int = Form(...),
    reason: str = Form(""),
    deny: str = Form("0"),
    actor: Actor = Depends(page_actor),
):
    with_denial = deny == "1"
    return act(
        actor,
        lambda: approval_service.revoke(
            actor.ctx, approval_id, reason, with_denial, expected_version=state_version
        ),
        "revoked_denied" if with_denial else "revoked", "/revocations",
        lambda **kw: revocations_page(request, actor, **kw),
    )


@router.post("/denials")
def create_denial(
    request: Request,
    scope_type: str = Form(...),
    scope_id: str = Form(...),
    server_id: str = Form(""),
    tools: str = Form(""),
    reason: str = Form(""),
    actor: Actor = Depends(page_actor),
):
    return act(
        actor,
        lambda: denial_service.deny(
            actor.ctx, scope_type=scope_type, scope_id=scope_id, server_id=server_id or None,
            tools=lines(tools) or None, reason=reason, org_id=None,
        ),
        "denied", "/revocations", lambda **kw: revocations_page(request, actor, **kw),
    )


@router.post("/denials/{denial_id}/lift")
def lift_denial(
    denial_id: str,
    request: Request,
    state_version: int = Form(...),
    actor: Actor = Depends(page_actor),
):
    return act(
        actor,
        lambda: denial_service.lift(actor.ctx, denial_id, expected_version=state_version),
        "lifted", "/revocations", lambda **kw: revocations_page(request, actor, **kw),
    )


# --- 호출별 승인 요청 -------------------------------------------------------------------------


def calls_page(request: Request, actor: Actor, error=None, status=200):
    rows = reads.call_approvals(actor.conn, actor.instance, page=page_number(request))
    return render(request, "call_approvals.html", {"rows": rows, "error": error}, actor=actor,
                  status=status)


@router.get("/call-approvals")
def call_approvals(request: Request, actor: Actor = Depends(page_actor)):
    return calls_page(request, actor)


@router.post("/call-approvals/{call_id}/{decision}")
def decide_call(
    call_id: str,
    decision: str,
    request: Request,
    state_version: int = Form(...),
    second_approver_token: str = Form(""),
    actor: Actor = Depends(page_actor),
):
    def action():
        if decision == "approve":
            second = api.second_approver(actor, second_approver_token)
            call_approval_service.approve(
                actor.ctx, call_id, second, expected_version=state_version
            )
        elif decision == "reject":
            call_approval_service.reject(actor.ctx, call_id, expected_version=state_version)
        elif decision == "cancel":
            call_approval_service.cancel(actor.ctx, call_id, expected_version=state_version)
        else:
            raise AdminError(AdminCode.NOT_FOUND, "알 수 없는 동작입니다.")

    ok = {"approve": "call_approved", "reject": "call_rejected", "cancel": "call_cancelled"}
    return act(actor, action, ok.get(decision, "call_approved"), "/call-approvals",
               lambda **kw: calls_page(request, actor, **kw))


# --- 원장 -----------------------------------------------------------------------------------


@router.get("/ledger")
def ledger(
    request: Request,
    since: str = "",
    until: str = "",
    kind: str = "",
    actor_name: str = Query("", alias="actor"),
    server_id: str = "",
    result: str = "",
    page: int = 1,
    actor: Actor = Depends(page_actor),
):
    def day(text: str, end: bool = False) -> datetime | None:
        if not text:
            return None
        parsed = datetime.fromisoformat(text).astimezone() if "T" in text else (
            datetime.fromisoformat(text).replace(tzinfo=datetime.now().astimezone().tzinfo)
        )
        return (parsed + timedelta(days=1)) if end and "T" not in text else parsed

    start = day(since) or datetime.now(UTC) - timedelta(days=reads.LEDGER_DEFAULT_DAYS)
    rows = reads.ledger(
        actor.conn, actor.instance, since=start, until=day(until, end=True), kind=kind or None,
        actor=actor_name or None, server_id=server_id or None, result=result or None, page=page,
    )
    query = {
        "since": since, "until": until, "kind": kind, "actor": actor_name, "server_id": server_id,
        "result": result,
    }
    ctx = {
        "rows": rows,
        "filters": reads.ledger_filters(actor.conn, actor.instance),
        "q": query,
        "start": start,
        "page": page,
        "servers": [s["server_id"] for s in reads.servers(actor.conn, actor.instance)],
    }
    return render(request, "ledger.html", ctx, actor=actor)


# --- 정책·관측 설정 ---------------------------------------------------------------------------


def policy_page(request: Request, actor: Actor, error=None, status=200):
    ctx = {
        "versions": reads.policy_versions(actor.conn, actor.instance),
        "s": reads.settings(actor.conn, actor.instance),
        "ceiling": actor.config.control.mode_ceiling.value,
        "error": error,
    }
    return render(request, "policy.html", ctx, actor=actor, status=status)


@router.get("/policy")
def policy(request: Request, actor: Actor = Depends(page_actor)):
    return policy_page(request, actor)


@router.post("/policy/mode")
def set_mode(
    request: Request,
    mode: str = Form(...),
    state_version: int = Form(...),
    actor: Actor = Depends(page_actor),
):
    return act(
        actor,
        lambda: settings_service.set_mode(
            actor.ctx, Mode(mode), actor.config.control.mode_ceiling, expected_version=state_version
        ),
        "mode_set", "/policy", lambda **kw: policy_page(request, actor, **kw),
    )


# --- 운영자·조직·허용 목록 ---------------------------------------------------------------------


def admin_page(request: Request, actor: Actor, error=None, status=200):
    ctx = {
        "operators": [] if actor.role == "viewer" else reads.operators(actor.conn, actor.instance),
        "orgs": reads.orgs(actor.conn, actor.instance),
        "bindings": reads.bindings(actor.conn, actor.instance),
        "allowlists": reads.allowlists(actor.conn, actor.instance),
        "error": error,
    }
    return render(request, "admin.html", ctx, actor=actor, status=status)


@router.get("/admin")
def admin(request: Request, actor: Actor = Depends(page_actor)):
    return admin_page(request, actor)


@router.post("/orgs")
def add_org(
    request: Request,
    org_id: str = Form(...),
    name: str = Form(...),
    actor: Actor = Depends(page_actor),
):
    return act(actor, lambda: org_service.add(actor.ctx, org_id, name), "org_added", "/admin",
               lambda **kw: admin_page(request, actor, **kw))


@router.post("/orgs/{org_id}/close")
def close_org(
    org_id: str,
    request: Request,
    state_version: int = Form(...),
    confirm_name: str = Form(""),
    actor: Actor = Depends(page_actor),
):
    def action():
        api.confirm(org_id, confirm_name, "조직")
        org_service.close(actor.ctx, org_id, expected_version=state_version)

    return act(actor, action, "org_closed", "/admin", lambda **kw: admin_page(request, actor, **kw))


@router.post("/allowlists")
def add_allowlist(
    request: Request,
    kind: str = Form(...),
    value: str = Form(...),
    valid_days: int = Form(90),
    note: str = Form(""),
    actor: Actor = Depends(page_actor),
):
    now = datetime.now(UTC)
    return act(
        actor,
        lambda: allowlist_service.add(
            actor.ctx, kind, value, now, now + timedelta(days=valid_days), None, note or None
        ),
        "allowlist_added", "/admin", lambda **kw: admin_page(request, actor, **kw),
    )


@router.post("/allowlists/{allowlist_id}/lift")
def lift_allowlist(
    allowlist_id: str,
    request: Request,
    state_version: int = Form(...),
    actor: Actor = Depends(page_actor),
):
    return act(
        actor,
        lambda: allowlist_service.lift(actor.ctx, allowlist_id, expected_version=state_version),
        "lifted", "/admin", lambda **kw: admin_page(request, actor, **kw),
    )
