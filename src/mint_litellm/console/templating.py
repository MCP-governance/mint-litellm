"""Jinja2 환경과 화면 공통 맥락(메뉴, 표시 시각, 상태 표시)."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from mint_litellm.console import labels

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")

MENU = (
    ("/", "개요"),
    ("/servers", "MCP 서버"),
    ("/approvals", "승인 대기열"),
    ("/revocations", "거부·철회"),
    ("/call-approvals", "호출별 승인"),
    ("/ledger", "원장"),
    ("/policy", "정책·관측 설정"),
    ("/admin", "운영자·조직"),
)
ICONS = {"ok": "✓", "wait": "◷", "error": "▲", "block": "⊘", "off": "–"}


def when(value: Any) -> str:
    """``연-월-일 시:분 (UTC±hh:mm)`` 형식. 시간대는 서버의 지역 시간대이다."""
    if value is None or value == "":
        return "—"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local = value.astimezone()
    offset = local.strftime("%z")
    return f"{local:%Y-%m-%d %H:%M} (UTC{offset[:3]}:{offset[3:]})"


def span(seconds: Any) -> str:
    """대기 시간을 한국어로(초·분·시간·일)."""
    if seconds is None:
        return "—"
    seconds = int(seconds)
    for limit, unit, size in ((60, "초", 1), (3600, "분", 60), (86400, "시간", 3600)):
        if seconds < limit:
            return f"{seconds // size}{unit}"
    return f"{seconds // 86400}일"


def blank(value: Any) -> Any:
    return "—" if value in (None, "", [], ()) else value


def join(values: Any) -> str:
    return ", ".join(values) if values else "—"


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


templates.env.filters.update(span=span, when=when, blank=blank, join=join, to_json=to_json)
templates.env.globals.update(L=labels, ICONS=ICONS, MENU=MENU)


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status: int = 200,
    actor: Any = None,
) -> HTMLResponse:
    notice = labels.NOTICES.get(request.query_params.get("ok", ""))
    base = {"actor": actor, "path": request.url.path, "notice": notice, "error": None}
    return templates.TemplateResponse(
        request, name, {**base, **(context or {})}, status_code=status
    )
