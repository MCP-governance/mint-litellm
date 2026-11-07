"""공개 MCP 서버 3종 실도입 시나리오 골격.

서버마다 같은 순서로 단계를 밟는다: 제출 -> 수용검증 -> 승인 -> 활성화 -> 호출 -> 판본 교체 ->
드리프트 차단 -> 재승인 -> 허용. 수용검증·계약·동기화 명령(운영 도구)은 아직 없을 수 있으므로
그 단계는 명령이 생기면 바로 돌도록 함수로만 두고, 없으면 「미구현으로 건너뜀」으로 표시한다.
명령이 생긴 뒤에도 본문을 채우지 않았으면 실패해서 채우도록 알린다.

스택은 stack.sh up으로 미리 올린다. 스택이 없으면 전부 건너뛰고, MINT_INTEGRATION_REQUIRED가
있으면 실패한다. 도커가 WSL에만 있으면 MINT_OSS_WSL_DISTRO에 배포판 이름을 준다.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

import measure
import oss_support as s
import pytest

# 단계 그룹별 운영 명령(최상위 이름). 확정 전 이름이므로 바뀌면 이 표만 고친다.
MINT_COMMANDS = {"validation": "intake", "contract": "contract", "sync": "sync"}

# 판본 교체로 바뀌어야 할 입력 속성: 서버 -> {도구: (사라지는 속성, 생기는 속성)}.
EXPECTED_CHANGE = {
    "playwright": {"browser_tabs": (set(), {"isolatedContext"})},
    "context7": {"query-docs": ({"researchMode"}, set())},
    "grafana": {
        "find_error_pattern_logs": ({"labels"}, {"labelSelector"}),
        "find_slow_requests": ({"labels"}, {"labelSelector"}),
    },
}


def mint_cli_has(command: str) -> bool:
    out = subprocess.run(
        [sys.executable, "-m", "mint_litellm.cli", command, "--help"], capture_output=True
    )
    return out.returncode == 0


def pending_step(group: str, step: str):
    """운영 명령이 없으면 건너뜀, 있는데 본문이 비었으면 실패."""
    command = MINT_COMMANDS[group]
    if not mint_cli_has(command):
        pytest.skip(f"미구현으로 건너뜀: {step} (명령 `{command}` 없음)")
    pytest.fail(f"{step}: 명령 `{command}`가 생겼다. 이 단계 본문을 채울 것")


@dataclass
class Flow:
    name: str
    admin: s.Litellm
    user: s.Litellm
    server_id: str = ""
    pinned: dict = field(default_factory=dict)
    swapped: bool = False

    @property
    def spec(self) -> dict:
        return self.admin.spec(self.name)

    def observed(self) -> list[dict]:
        resp = self.admin.call("POST", "/mcp-rest/test/tools/list", json=self.spec)
        return measure.tools_of(measure.body(resp))


@pytest.fixture(scope="session")
def admin():
    client = s.Litellm()
    if not client.ready():
        message = "시험 스택이 올라와 있지 않음: tests/oss/stack.sh up"
        if os.environ.get("MINT_INTEGRATION_REQUIRED"):
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    measure.purge(client, {i["alias"] for i in s.SERVERS.values()})
    return client


@pytest.fixture(scope="session")
def submitter(admin):
    return s.Litellm(admin.new_user_key()[1])


@pytest.fixture(scope="module", params=list(s.SERVERS))
def flow(request, admin, submitter):
    name = request.param
    state = Flow(name, admin, submitter)
    yield state
    if state.swapped:
        s.stack("swap", name, s.VERSIONS[name][0], check=False)
    if state.server_id:
        admin.call("DELETE", f"/v1/mcp/server/{state.server_id}/pin")
        admin.call("DELETE", f"/v1/mcp/server/{state.server_id}")


def test_01_submit(flow):
    """비관리자 팀 키로 제출하면 검토 대기로 들어간다."""
    resp = flow.user.call("POST", "/v1/mcp/server/register", json=flow.spec)
    assert resp.status_code in (200, 201), resp.text[:300]
    data = resp.json()
    assert data["approval_status"] == "pending_review"
    assert data["submitted_by"]
    flow.server_id = data["server_id"]


def test_02_validation(flow):
    """수용검증: 시험 연결·도구 목록 수집·설명 검사·위험 등급."""
    pending_step("validation", "수용검증")


def test_03_approval(flow):
    """승인: 신청자와 다른 운영자가 승인한다."""
    pending_step("validation", "승인")


def test_04_activation(flow):
    """활성화: 서버를 레지스트리에 올리고 도구 정의를 고정한다(운영 도구 없이 가능)."""
    assert flow.admin.call("PUT", f"/v1/mcp/server/{flow.server_id}/approve").status_code == 200
    pin = flow.admin.call("POST", f"/v1/mcp/server/{flow.server_id}/pin")
    assert pin.status_code == 200, pin.text[:300]
    flow.pinned = pin.json()
    observed = {t["name"] for t in flow.observed()}
    assert set(flow.pinned) == observed, "고정한 도구 집합이 업스트림 목록과 다름"


def test_05_call_allowed(flow):
    """호출: 활성화된 서버의 읽기 도구가 LiteLLM을 거쳐 성공한다."""
    tool, args = measure.READ_ONLY_CALL[flow.name]
    resp = flow.admin.call(
        "POST",
        "/mcp-rest/tools/call",
        json={"server_id": flow.server_id, "name": tool, "arguments": args},
    )
    assert resp.status_code == 200, resp.text[:300]
    assert resp.json()["isError"] is False, resp.text[:300]


def test_06_version_swap(flow):
    """판본 교체: 새 판본의 업스트림 정의가 설계가 말한 대로 바뀌고, 고정본은 그대로다."""
    old_v, new_v = s.VERSIONS[flow.name]
    before = {t["name"]: t for t in flow.observed()}
    flow.swapped = True
    s.stack("swap", flow.name, new_v)
    measure.wait_ready(flow.admin, flow.spec)
    after = {t["name"]: t for t in flow.observed()}
    for tool, (gone, new) in EXPECTED_CHANGE[flow.name].items():
        old_props = set((before[tool].get("inputSchema") or {}).get("properties", {}))
        new_props = set((after[tool].get("inputSchema") or {}).get("properties", {}))
        assert gone <= old_props - new_props, (tool, "사라져야 할 속성", gone, old_props, new_props)
        assert new <= new_props - old_props, (tool, "생겨야 할 속성", new, old_props, new_props)
    listed = flow.admin.call("GET", "/mcp-rest/tools/list", params={"server_id": flow.server_id})
    served = {t["name"]: t for t in measure.tools_of(measure.body(listed))}
    assert set(served) == set(flow.pinned), "고정 뒤 목록은 고정본이어야 함"
    for tool in EXPECTED_CHANGE[flow.name]:
        assert json.dumps(served[tool].get("inputSchema"), sort_keys=True) == json.dumps(
            flow.pinned[tool]["input_schema"], sort_keys=True
        ), (tool, "고정본과 다른 정의가 나감")


def test_07_drift_blocked(flow):
    """드리프트 차단: 다음 관측 뒤 바뀐 도구 호출 0회, 다른 도구는 허용."""
    pending_step("contract", "드리프트 차단")


def test_08_reapproval(flow):
    """재승인: 비교 화면에서 승인, 재고정 해시 일치."""
    pending_step("contract", "재승인")


def test_09_allowed_after_reapproval(flow):
    """재승인 뒤 허용 1회."""
    pending_step("contract", "재승인 뒤 허용")


def test_10_permission_sync(flow):
    """권한 동기화: LiteLLM에서 MINT 소유 권한을 지우면 재부여, 반복하면 보류."""
    pending_step("sync", "권한 동기화")
