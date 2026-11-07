"""되돌림 감시: 60초마다 내보내기 파일과 DB를 대조해 결과를 메모리에 둔다.

호출 판정(핫패스)은 ``blocked_cause``만 읽는다. 파일 읽기와 DB 조회는 이 모듈의 주기 작업만 한다.
2단계 스키마(``mint.exports``)가 없는 DB(1단계 배포)에서는 아무것도 하지 않아 동작이 바뀌지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from mint_litellm.plugin.config import PluginConfig
from mint_litellm.plugin.snapshot import PluginDatabase
from mint_litellm.restore import judge

log = logging.getLogger("mint_litellm.plugin.restore")


@dataclass
class RestoreWatch:
    config: PluginConfig
    database: PluginDatabase
    blocked_cause: str | None = None
    verdict: judge.Verdict | None = None
    judge: judge.Judge = field(default_factory=judge.Judge)

    @property
    def keys(self) -> list[bytes]:
        c = self.config
        return [k for k in (c.export_hmac_key, c.export_hmac_key_previous) if k]

    async def _row(self, sql: str, params: Any) -> dict[str, Any] | None:
        from psycopg.rows import dict_row

        async def read(conn: Any) -> Any:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(sql, params)
                return await cursor.fetchone()

        return await self.database._run(read)

    async def refresh(self) -> judge.Verdict | None:
        """한 번 판정해 ``blocked_cause``를 갱신한다. DB 읽기에 실패하면 이전 값을 유지한다."""
        try:
            applied = await self._row("SELECT to_regclass('mint.exports') IS NOT NULL AS ok", ())
            if not applied or not applied["ok"]:
                self.blocked_cause, self.verdict = None, None  # 1단계: 판정 대상이 아니다
                return None
            c = self.config
            if c.export_dir is None or not c.export_hmac_key:
                self.verdict = judge.Verdict(judge.BLOCKED, judge.EXPORT_CONFIG_MISSING)
            else:
                state = await asyncio.to_thread(
                    judge.read_files, c.export_dir, c.instance_id, self.keys
                )
                facts = None
                if state.error != "unverified":
                    params = judge.facts_params(state, c.instance_id)
                    facts = await self._row(judge.FACTS_SQL, params)
                self.verdict = self.judge.decide(state, facts)
            self.blocked_cause = self.verdict.cause if self.verdict.blocked else None
        except Exception as error:
            log.error("되돌림 판정 오류: %s", type(error).__name__)
        return self.verdict

    async def run(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.config.restore_interval_seconds)
