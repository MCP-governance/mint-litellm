"""세 공개 MCP 서버를 LiteLLM에 제출했을 때의 동작을 실측해 JSON으로 낸다.

사용: python tests/oss/measure.py [출력 파일]   (먼저 stack.sh up)
기록: 시험 경로(연결·도구 목록), 비관리자 제출, 제출 값 allow_all_keys 처리, 승인 전후 목록,
도구 호출, 자격이 필요한 서버의 대조군, 판본 교체 뒤 도구 정의 차이.
키·토큰은 출력에 넣지 않는다. 끝나면 서버 판본을 처음 값으로 되돌린다.
"""

import json
import sys
import time

import oss_support as s

READ_ONLY_CALL = {
    "grafana": ("list_datasources", {}),
    "context7": ("resolve-library-id", {"libraryName": "react", "query": "hooks"}),
    "playwright": ("browser_tabs", {"action": "list"}),
}


def body(resp) -> object:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:300]


def tools_of(payload) -> list[dict]:
    return (payload.get("tools") or []) if isinstance(payload, dict) else []


def short(payload, limit: int = 300) -> str:
    return s.redact(json.dumps(payload, ensure_ascii=False))[:limit]


def purge(admin: s.Litellm, aliases: set[str]) -> None:
    """이전 실행이 남긴 같은 별칭의 서버를 지운다(승인 전 제출은 제출 목록에만 있다)."""
    for path in ("/v1/mcp/server", "/v1/mcp/server/submissions"):
        listed = body(admin.call("GET", path))
        if isinstance(listed, dict):
            listed = listed.get("items") or listed.get("servers") or []
        for item in listed if isinstance(listed, list) else []:
            if item.get("alias") in aliases:
                admin.call("DELETE", f"/v1/mcp/server/{item['server_id']}")


def test_paths(admin: s.Litellm, spec: dict) -> dict:
    conn = admin.call("POST", "/mcp-rest/test/connection", json=spec)
    listed = admin.call("POST", "/mcp-rest/test/tools/list", json=spec)
    payload = body(listed)
    out = {
        "connection": {"http": conn.status_code, "body": short(body(conn), 120)},
        "tools_list": {"http": listed.status_code, **s.summarize_tools(tools_of(payload))},
    }
    if listed.status_code != 200 or not tools_of(payload):
        out["tools_list"]["body"] = short(payload)
    return out


def count(resp) -> dict:
    return {"http": resp.status_code, "tools": len(tools_of(body(resp)))}


def measure_server(admin: s.Litellm, user: s.Litellm, name: str) -> dict:
    spec = admin.spec(name)
    out: dict = {"server": name, **test_paths(admin, spec)}
    sub = user.call("POST", "/v1/mcp/server/register", json=spec)
    sub_body = body(sub)
    ok = sub.status_code in (200, 201) and isinstance(sub_body, dict)
    out["submission"] = {
        "http": sub.status_code,
        "approval_status": sub_body.get("approval_status") if ok else None,
        "submitted_by_set": bool(sub_body.get("submitted_by")) if ok else None,
        "allow_all_keys": sub_body.get("allow_all_keys") if ok else None,
    }
    if not ok:
        out["submission"]["body"] = short(sub_body)
        return out
    server_id = sub_body["server_id"]
    out["server_id"] = server_id
    where = {"server_id": server_id}
    out["admin_list_before_approval"] = count(
        admin.call("GET", "/mcp-rest/tools/list", params=where)
    )
    out["approve"] = {"http": admin.call("PUT", f"/v1/mcp/server/{server_id}/approve").status_code}
    health = body(admin.call("GET", "/v1/mcp/server/health", params={"server_ids": server_id}))
    out["health"] = short(health, 120)
    out["admin_list_after_approval"] = count(
        admin.call("GET", "/mcp-rest/tools/list", params=where)
    )
    out["submitter_list_after_approval"] = count(
        user.call("GET", "/mcp-rest/tools/list", params=where)
    )
    tool, args = READ_ONLY_CALL[name]
    call = admin.call(
        "POST",
        "/mcp-rest/tools/call",
        json={"server_id": server_id, "name": tool, "arguments": args},
    )
    call_body = body(call)
    out["call"] = {
        "tool": tool,
        "http": call.status_code,
        "isError": call_body.get("isError") if isinstance(call_body, dict) else None,
    }
    return out


def measure_allow_all(admin: s.Litellm, user: s.Litellm) -> dict:
    """제출 값 allow_all_keys=true가 저장·승인에서 그대로 유지되는지."""
    alias = "oss_allowall_probe"
    spec = admin.spec("context7", alias=alias, server_name=alias, allow_all_keys=True)
    sub = user.call("POST", "/v1/mcp/server/register", json=spec)
    sub_body = body(sub)
    if sub.status_code not in (200, 201):
        return {"http": sub.status_code, "body": short(sub_body)}
    server_id = sub_body["server_id"]
    stored = body(admin.call("GET", "/v1/mcp/server/submissions"))
    row = next((i for i in stored.get("items", []) if i["server_id"] == server_id), {})
    approve = admin.call("PUT", f"/v1/mcp/server/{server_id}/approve")
    after = body(admin.call("GET", f"/v1/mcp/server/{server_id}"))
    admin.call("DELETE", f"/v1/mcp/server/{server_id}")
    return {
        "submitted": sub_body.get("allow_all_keys"),
        "stored_before_approval": row.get("allow_all_keys"),
        "approve_http": approve.status_code,
        "stored_after_approval": after.get("allow_all_keys") if isinstance(after, dict) else None,
    }


def measure_credentials(admin: s.Litellm) -> dict:
    """자격이 필요한 서버(서버 인증 토큰을 켠 컨테이너)를 시험 경로에 보냈을 때."""
    s.stack("authprobe", "up")
    token = s.runtime_env().get("G10_AUTHPROBE_TOKEN", "")
    alias = "oss_authprobe"
    spec = admin.spec(
        "grafana", alias=alias, server_name=alias, url="http://mint-g10-authprobe:8000/mcp"
    )
    time.sleep(3)
    try:
        none = test_paths(admin, spec)
        bearer = {**spec, "auth_type": "bearer_token"}
        wrong = test_paths(admin, {**bearer, "credentials": {"auth_value": "wrong-token"}})
        right = test_paths(admin, {**bearer, "credentials": {"auth_value": token}})
    finally:
        s.stack("authprobe", "down", check=False)
    return {"no_credentials": none, "wrong_token": wrong, "right_token": right}


def wait_ready(admin: s.Litellm, spec: dict, seconds: int = 120) -> None:
    """시험 경로는 연결 실패도 HTTP 200으로 답하므로 본문의 상태가 ok일 때까지 기다린다."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        resp = admin.call("POST", "/mcp-rest/test/connection", json=spec)
        if resp.status_code == 200 and body(resp).get("status") == "ok":
            return
        time.sleep(3)
    sys.exit(f"판본 교체 뒤 응답 없음: {spec['url']}")


def schema_diff(old: list[dict], new: list[dict]) -> dict:
    a = {t["name"]: t for t in old}
    b = {t["name"]: t for t in new}
    both = set(a) & set(b)
    desc = lambda t: t.get("description") or ""  # noqa: E731
    return {
        "added": sorted(set(b) - set(a)),
        "removed": sorted(set(a) - set(b)),
        "input_schema_changed": sorted(
            n for n in both if s.tool_signature(a[n]) != s.tool_signature(b[n])
        ),
        "description_changed": sorted(n for n in both if desc(a[n]) != desc(b[n])),
    }


def measure_swap(admin: s.Litellm, name: str) -> dict:
    old_v, new_v = s.VERSIONS[name]
    spec = admin.spec(name)
    before = tools_of(body(admin.call("POST", "/mcp-rest/test/tools/list", json=spec)))
    s.stack("swap", name, new_v)
    try:
        wait_ready(admin, spec)
        after = tools_of(body(admin.call("POST", "/mcp-rest/test/tools/list", json=spec)))
    finally:
        s.stack("swap", name, old_v)
        wait_ready(admin, spec)
    return {
        "server": name,
        "from": old_v,
        "to": new_v,
        "tools_from": len(before),
        "tools_to": len(after),
        **schema_diff(before, after),
    }


def main() -> None:
    admin = s.Litellm()
    if not admin.ready():
        sys.exit("LiteLLM이 준비되지 않음: stack.sh up")
    purge(admin, {i["alias"] for i in s.SERVERS.values()} | {"oss_allowall_probe"})
    _, user_key = admin.new_user_key()
    user = s.Litellm(user_key)
    result = {"servers": [measure_server(admin, user, n) for n in s.SERVERS]}
    result["allow_all_keys_probe"] = measure_allow_all(admin, user)
    result["credentials_probe"] = measure_credentials(admin)
    result["version_swap"] = [measure_swap(admin, n) for n in s.SERVERS]
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
