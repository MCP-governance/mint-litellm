"""통합 시험 공통 픽스처.

스택은 ``docker compose -f tests/integration/compose.yaml up --wait``로 미리 띄운다.
스택이 없으면 통합 시험은 건너뛴다. 아직 병합되지 않은 구성요소가 필요한 시험은
``MINT_E_READY``(쉼표로 구분한 이름, 예: ``plugin,control,policy``)에 이름이 있어야 실행된다.
"""

import os

import httpx
import pytest
import support

COMPONENTS = ("plugin", "control", "policy")


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: 실제 스택이 필요한 통합 시험")
    for name in COMPONENTS:
        config.addinivalue_line("markers", f"needs_{name}: {name} 구성요소가 병합된 뒤에만 실행")


def pytest_collection_modifyitems(config, items):
    ready = {x.strip() for x in os.environ.get("MINT_E_READY", "").split(",") if x.strip()}
    for item in items:
        if "tests/integration" not in item.nodeid.replace("\\", "/"):
            continue
        item.add_marker(pytest.mark.integration)
        for name in COMPONENTS:
            if item.get_closest_marker(f"needs_{name}") and name not in ready:
                item.add_marker(
                    pytest.mark.skip(reason=f"{name} 구성요소가 아직 없음(MINT_E_READY)")
                )


def _readiness() -> dict[str, bool]:
    checks = {}
    for index, port in enumerate(support.LITELLM_PORTS, 1):
        checks[f"litellm{index}"] = (port, "/health/readiness")
    checks["counting-proxy"] = (support.PROXY_PORT, "/__health")
    checks["toxiproxy"] = (support.TOXIPROXY_PORT, "/proxies")
    checks["toxiproxy-db"] = (support.TOXIPROXY_DB_PORT, "/proxies")
    checks["opa"] = (support.OPA_PORT, "/health")
    for name, port in support.UPSTREAM_CONTROL_PORT.items():
        checks[f"testserver-{name}"] = (port, "/health")
    result = {}
    for name, (port, path) in checks.items():
        try:
            result[name] = httpx.get(support.url(port, path), timeout=3).status_code == 200
        except httpx.HTTPError:
            result[name] = False
    try:
        import psycopg

        with psycopg.connect(support.mint_database_url(), connect_timeout=3) as conn:
            conn.execute("select 1")
        result["postgres"] = True
    except Exception:
        result["postgres"] = False
    return result


@pytest.fixture(scope="session")
def stack():
    status = _readiness()
    missing = [name for name, ok in status.items() if not ok]
    if missing:
        pytest.skip(f"통합 스택이 준비되지 않음: {', '.join(missing)}")
    return status


@pytest.fixture
def upstreams(stack):
    servers = {name: support.Upstream(name) for name in support.UPSTREAM_CONTROL_PORT}
    for upstream in servers.values():
        upstream.reset()
    yield servers
    for upstream in servers.values():
        upstream.reset()


@pytest.fixture
def proxy_log(stack):
    return support.ProxyLog()


@pytest.fixture
def evidence(upstreams, proxy_log):
    return support.Evidence(upstreams, proxy_log)


@pytest.fixture
def toxics(stack):
    created = [
        support.Toxics(support.TOXIPROXY_PORT, "a"),
        support.Toxics(support.TOXIPROXY_PORT, "b"),
        support.Toxics(support.TOXIPROXY_DB_PORT, "postgres"),
    ]
    for toxic in created:
        toxic.clear()
        toxic.enable()
    yield {"a": created[0], "b": created[1], "postgres": created[2]}
    for toxic in created:
        toxic.clear()
        toxic.enable()


@pytest.fixture(scope="session")
def world(stack):
    """MINT 플러그인과 제어 평면이 붙은 스택에서 첫 관리자와 제어 평면 키를 만든다."""
    import scenario

    mint = scenario.Mint()
    helper = scenario.World(mint)
    admin_user = helper.litellm_user()
    mint.token = mint.data("operator", "bootstrap", "--name", "it-admin", "--user-id", admin_user)[
        "token"
    ]
    proxy_admin = httpx.post(
        support.url(support.LITELLM_PORTS[0], "/user/new"),
        headers=support.master_headers(),
        json={"user_id": "it-control", "user_role": "proxy_admin"},
        timeout=30,
    )
    proxy_admin.raise_for_status()
    routes = [
        "/v1/mcp/server",
        "/key/info",
        "/team/info",
        "/user/info",
        "/key/list",
        "/team/list",
        "/guardrails/list",
    ]
    reply = httpx.post(
        support.url(support.LITELLM_PORTS[0], "/key/generate"),
        headers=support.master_headers(),
        json={"user_id": "it-control", "key_alias": "it-control-key", "allowed_routes": routes},
        timeout=30,
    )
    reply.raise_for_status()
    mint.env["MINT_LITELLM_KEY"] = reply.json()["key"]
    helper.control_key = reply.json()["key"]
    return helper
