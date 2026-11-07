"""수집 때는 DB, 네트워크, Docker, 제품 초기화를 실행하지 않는다."""

import json
import os
import uuid
from pathlib import Path

import pytest

from .rig import Rig


def pytest_addoption(parser):
    group = parser.getgroup("stage2")
    group.addoption("--stage2-live", action="store_true", help="격리된 장비 시험 실행")
    group.addoption("--stage2-required", action="store_true", help="건너뜀도 실패로 판정")
    group.addoption("--stage2-site", default=os.environ.get("MINT_ACC_SITE"))
    group.addoption("--stage2-evidence", default="tests/acceptance/stage2/evidence")


def pytest_configure(config):
    config._stage2_batch = uuid.uuid4().hex


@pytest.fixture
def rig(request):
    root = Path(request.config.getoption("--stage2-evidence"))
    test = Rig(request.node.nodeid, root)
    test.result["batch_id"] = request.config._stage2_batch
    try:
        if not request.config.getoption("--stage2-live"):
            test.skip("장비 실행 비활성: --stage2-live 필요")
        path = request.config.getoption("--stage2-site")
        if not path:
            test.skip("장비 설정 없음: --stage2-site 필요")
        filename = request.node.path.name
        gate = test.expected["capabilities"].get(filename, {})
        test.require(*gate.get("commands", []), handlers=gate.get("handlers", []))
        test.configure(json.loads(Path(path).read_text(encoding="utf-8")))
        yield test
    except pytest.skip.Exception as error:
        test.result["status"] = "skipped"
        test.result["reason"] = str(error)
        if request.config.getoption("--stage2-required"):
            test.result["status"] = "failed"
            pytest.fail(str(error), pytrace=False)
        raise
    except BaseException as error:
        test.result["status"] = "failed"
        test.result["reason"] = test.clean(str(error))
        raise
    finally:
        test.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    test = item.funcargs.get("rig")
    if test and report.when == "call":
        if report.skipped and item.config.getoption("--stage2-required"):
            report.outcome = "failed"
            report.longrepr = "필수 장비 시험이 건너뛰어졌습니다: " + str(report.longrepr)
        test.result["status"] = report.outcome
        test.save()
