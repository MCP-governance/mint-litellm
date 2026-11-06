"""만료 검사 작업이 쓰는 서비스.

작업 실행기의 로그인 역할은 제어 그룹을 함께 받아 데이터베이스가 전이를 더 좁히지 못하므로,
``expired``로만 바꾸는 규칙은 이 계층의 문장이 지킨다.
"""

import psycopg


def expire_due(conn: psycopg.Connection, instance_id: str) -> dict[str, int]:
    """만료 시각이 지난 호출별 승인과 본인 확인을 ``expired``로 바꾼다. 바꾼 행 수를 돌려준다."""
    with conn.transaction():
        approvals = conn.execute(
            "UPDATE mint.call_approvals SET status = 'expired' WHERE instance_id = %s"
            " AND status IN ('pending', 'approved') AND expires_at <= now()",
            (instance_id,),
        ).rowcount
        confirmations = conn.execute(
            "UPDATE mint.self_confirmations SET status = 'expired' WHERE instance_id = %s"
            " AND status IN ('pending', 'confirmed') AND expires_at <= now()",
            (instance_id,),
        ).rowcount
    return {"call_approvals": approvals, "self_confirmations": confirmations}
