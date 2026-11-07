"""9. 자격 미저장, 세션/CSRF/RBAC, DB 역할과 실제 화면 증거."""

import pytest


@pytest.mark.parametrize("case", ["no_plaintext", "blocked_key", "deleted_key", "unbound_viewer",
                                     "viewer_write", "staff_write", "plugin_control_event",
                                     "csrf", "escape", "screens_13"])
def test_authentication_and_ui(rig, case):
    rig.require("console", handlers=("session_verify",))
    rig.fault("auth_" + case)
    observation = rig.read_observation("auth_" + case)
    rig.check_observation("auth_" + case, observation)
    if case == "screens_13":
        # 화면 검수는 URL 도달이나 HTML 검색만으로 통과시키지 않는다.
        screenshots = observation["screenshots"]
        assert len(screenshots) == 13
        for screenshot in screenshots:
            rig.verify_artifact(screenshot)
        assert observation["reviewer"]
    if case == "no_plaintext":
        assert set(observation["searched_locations"]) == {"database", "logs", "sessions", "buffer"}
        assert observation["sentinel_sha256"]
