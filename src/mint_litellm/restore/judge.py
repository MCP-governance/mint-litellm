"""되돌림 판정(내용 대조). 플러그인과 작업 실행기가 같은 함수를 쓴다.

판정은 세 단계다. ① 파일을 읽고 서명을 확인한다(``read_files``, 파일 입출력). ② DB에서 사실을
한 문장으로 읽는다(``FACTS_SQL``, 동기·비동기 연결 어느 쪽이든). ③ 순수 함수 ``Judge.decide``가
네 경우(파일 없음·읽기 실패·내용 불일치·정상)를 판정한다.

상태: ``skip``(판정 안 함, 첫 설치), ``ok``, ``hold``(내용 대조가 처음 어긋남: 작업 실행기만
봉인·내보내기를 보류하고 호출은 막지 않는다), ``blocked``(모든 호출 차단).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from mint_litellm.control.db.connection import fetchone
from mint_litellm.restore import files

SKIP, OK, HOLD, BLOCKED = "skip", "ok", "hold", "blocked"
RESTORE_PENDING = "restore_pending"
EXPORT_MISSING = "export_missing"
EXPORT_UNVERIFIED = "export_unverified"
EXPORT_CONFIG_MISSING = "export_config_missing"

# 부정 상태 항목은 행이 있고 state_version이 파일 값 이상이며 상태 열도 파일과 같아야 한다.
# (값이 안 바뀐 UPDATE도 버전을 올리므로 버전만으로는 되돌림을 가릴 수 있다. 정상 해제·변경은
# 새 부정 상태 파일을 내므로 파일이 따라온다.)
FACTS_SQL = """
WITH f AS (
  SELECT * FROM jsonb_to_recordset(%(items)s::jsonb)
    AS x(kind text, id text, state_version bigint, state text)
), rows AS (
  SELECT 'approval' AS kind, id::text AS id, state_version, status AS state FROM mint.approvals
    WHERE instance_id = %(i)s AND id::text IN (SELECT id FROM f WHERE kind = 'approval')
  UNION ALL SELECT 'denial', id::text, state_version,
      CASE WHEN lifted_at IS NULL THEN 'active' ELSE 'lifted' END FROM mint.denials
    WHERE instance_id = %(i)s AND id::text IN (SELECT id FROM f WHERE kind = 'denial')
  UNION ALL SELECT 'server', server_id, state_version, status FROM mint.servers
    WHERE instance_id = %(i)s AND server_id IN (SELECT id FROM f WHERE kind = 'server')
  UNION ALL SELECT 'relation', id::text, state_version, status FROM mint.usage_relations
    WHERE instance_id = %(i)s AND id::text IN (SELECT id FROM f WHERE kind = 'relation')
  UNION ALL SELECT 'org', org_id, state_version, status FROM mint.orgs
    WHERE instance_id = %(i)s AND org_id IN (SELECT id FROM f WHERE kind = 'org')
  UNION ALL SELECT 'allowlist', id::text, state_version, status FROM mint.allowlists
    WHERE instance_id = %(i)s AND id::text IN (SELECT id FROM f WHERE kind = 'allowlist')
  UNION ALL SELECT 'consent', id::text, state_version,
      CASE WHEN revoked_at IS NULL THEN 'active' ELSE 'revoked' END FROM mint.consents
    WHERE instance_id = %(i)s AND id::text IN (SELECT id FROM f WHERE kind = 'consent')
)
SELECT (SELECT epoch FROM mint.state_epoch WHERE instance_id = %(i)s) AS epoch,
  EXISTS (SELECT 1 FROM mint.exports WHERE instance_id = %(i)s AND status = 'written')
    AS has_written,
  EXISTS (SELECT 1 FROM mint.exports WHERE instance_id = %(i)s AND status = 'written'
          AND kind = 'negative_state') AS has_negative_written,
  (SELECT count(*) FROM f WHERE NOT EXISTS (
     SELECT 1 FROM rows r
     WHERE r.kind = f.kind AND r.id = f.id AND r.state_version >= f.state_version
       AND r.state = f.state
  )) AS missing,
  CASE WHEN %(seq)s::bigint IS NULL THEN true ELSE EXISTS (
    SELECT 1 FROM mint.seal_batches WHERE instance_id = %(i)s AND to_seal_seq = %(seq)s::bigint
      AND encode(tail_hash, 'hex') = %(tail)s) END AS anchor_ok
"""


@dataclass(frozen=True)
class Verdict:
    state: str
    cause: str | None = None
    file_epoch: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.state == BLOCKED

    @property
    def held(self) -> bool:
        """봉인·내보내기를 멈춰야 하는가(처음 어긋남 또는 차단)."""
        return self.state in (HOLD, BLOCKED)


@dataclass(frozen=True)
class FileState:
    latest: dict[str, Any] | None = None
    negative: dict[str, Any] | None = None
    error: str | None = None  # None | 'missing' | 'unverified'


def read_files(export_dir: Path, instance_id: str, keys: Sequence[bytes]) -> FileState:
    try:
        latest = files.read_verified(files.latest_path(export_dir, instance_id), keys)
    except FileNotFoundError:
        return FileState(error="missing")
    except files.ExportUnverified:
        return FileState(error="unverified")
    negative = None
    path = files.newest_negative(export_dir, instance_id)
    if path is not None:
        try:
            negative = files.read_verified(path, keys)
        except (FileNotFoundError, files.ExportUnverified):
            return FileState(latest=latest, error="unverified")
    return FileState(latest=latest, negative=negative)


def facts_params(state: FileState, instance_id: str) -> dict[str, Any]:
    latest, negative = state.latest or {}, state.negative or {}
    return {
        "i": instance_id,
        "items": Jsonb(negative.get("items") or []),
        "seq": latest.get("to_seal_seq"),
        "tail": latest.get("tail_hash"),
    }


class Judge:
    """파일 상태와 DB 사실로 판정한다. 내용 불일치 횟수(연속)를 기억한다."""

    def __init__(self) -> None:
        self.misses = 0

    def decide(self, state: FileState, facts: dict[str, Any] | None) -> Verdict:
        if state.error == "unverified":
            self.misses = 0
            return Verdict(BLOCKED, EXPORT_UNVERIFIED)
        assert facts is not None
        if state.error == "missing":
            self.misses = 0
            if facts["has_written"]:
                return Verdict(BLOCKED, EXPORT_MISSING)
            return Verdict(SKIP)
        assert state.latest is not None
        if state.negative is None and facts["has_negative_written"]:
            self.misses = 0  # 부정 상태 파일이 사라졌다(볼륨 누락·디렉터리 교체)
            return Verdict(BLOCKED, EXPORT_MISSING)
        file_epoch = int(state.latest["state_epoch"])
        epoch = facts["epoch"] if facts["epoch"] is not None else 0
        detail = {
            "missing_items": int(facts["missing"]),
            "anchor_ok": bool(facts["anchor_ok"]),
            "db_epoch": epoch,
            "file_epoch": file_epoch,
        }
        # DB 세대가 파일보다 작은 것은 보조 신호다(복구 뒤 소비로 추월하면 보이지 않는다).
        mismatch = detail["missing_items"] > 0 or not detail["anchor_ok"] or epoch < file_epoch
        if not mismatch:
            self.misses = 0
            return Verdict(OK, file_epoch=file_epoch, detail=detail)
        self.misses += 1
        state_name = BLOCKED if self.misses >= 2 else HOLD
        return Verdict(state_name, RESTORE_PENDING, file_epoch, detail)

    def evaluate(self, conn, export_dir: Path, instance_id: str, keys: Sequence[bytes]) -> Verdict:
        """동기 연결용 한 번의 판정."""
        state = read_files(export_dir, instance_id, keys)
        facts = None
        if state.error != "unverified":
            facts = fetchone(conn, FACTS_SQL, facts_params(state, instance_id))
        return self.decide(state, facts)
