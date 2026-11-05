"""통합 시험 공통 픽스처.

스택은 ``docker compose -f tests/integration/compose.yaml up --wait``로 미리 띄운다.
스택이 없으면 통합 시험은 건너뛴다. 플러그인이 없는 순정 이미지(프로필 ``plain``)가 필요한
시험은 그 서비스가 떠 있을 때만 실행된다.
"""

import httpx
import pytest
import support


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: 실제 스택이 필요한 통합 시험")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "tests/integration" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.integration)


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
def plain(stack):
    port = support.PLAIN_PORT
    try:
        ready = httpx.get(support.url(port, "/health/readiness"), timeout=3).status_code == 200
    except httpx.HTTPError:
        ready = False
    if not ready:
        pytest.skip("순정 LiteLLM(프로필 plain)이 떠 있지 않음")
    return port


@pytest.fixture(scope="session")
def world(stack):
    """첫 관리자와 제어 평면 키를 만들고 첫 정책 판본을 활성화한다."""
    import scenario

    mint = scenario.Mint()
    helper = scenario.World(mint)
    reply = httpx.post(
        support.url(support.LITELLM_PORTS[0], "/user/new"),
        headers=support.master_headers(),
        json={"user_id": "it-control", "user_role": "proxy_admin"},
        timeout=30,
    )
    reply.raise_for_status()
    reply = httpx.post(
        support.url(support.LITELLM_PORTS[0], "/key/generate"),
        headers=support.master_headers(),
        json={
            "user_id": "it-control",
            "key_alias": "it-control-key",
            "allowed_routes": support.CONTROL_ROUTES,
        },
        timeout=30,
    )
    reply.raise_for_status()
    mint.env["MINT_LITELLM_KEY"] = reply.json()["key"]
    helper.control_key = reply.json()["key"]
    mint.env["MINT_LITELLM_URL"] = "http://litellm1:4000"
    admin_user = helper.litellm_user()
    mint.token = mint.data("operator", "bootstrap", "--name", "it-admin", "--user-id", admin_user)[
        "token"
    ]
    helper.admin_user = admin_user
    mint.run("policy", "activate", "it-v1")
    return helper
