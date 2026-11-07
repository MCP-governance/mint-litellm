"""12. 실제 v001 데이터가 든 DB의 이행과 수용검증 우회 차단."""


def test_migration(rig):
    rig.require("intake create", handlers=("intake_run",))
    # 별도 이행 스택을 이용한다. 운영 중인 판정 스택의 DB를 덮어쓰지 않는다.
    rig.fault("migration_v001")
    observation = rig.read_observation("migration_v001")
    rig.check_observation("migration_v001", observation)
    assert observation["before_schema"] == "v001"
    assert observation["after_schema"] == "v002"
    assert observation["stage1_rows_before"] > 0
    assert observation["stage1_rows_after"] >= observation["stage1_rows_before"]
    assert observation["legacy_contract_status"] == "none"
