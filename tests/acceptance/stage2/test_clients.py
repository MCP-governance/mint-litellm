"""항목 1·2의 실제 Claude Code 단계만 따로 실행한다(ACC_CLIENTS_ONLY=1). 전체 실행에서는 건너뛴다."""

import os

import pytest

from . import client_flow


@pytest.mark.skipif(not os.environ.get("ACC_CLIENTS_ONLY"), reason="ACC_CLIENTS_ONLY 필요")
def test_clients_only(rig):
    client_flow.run(rig)
