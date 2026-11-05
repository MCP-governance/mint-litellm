import os
import stat

import pytest

from mint_litellm.plugin.policy.bundle import publish_bundle


@pytest.mark.skipif(os.name == "nt", reason="POSIX 파일 권한 시험")
def test_published_bundle_is_readable_by_other_users(tmp_path):
    target = tmp_path / "mint.tar.gz"
    publish_bundle(target, b"data")
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    publish_bundle(target, b"newer")
    assert target.read_bytes() == b"newer"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
