-- 직원 세션이 로그인 때 확인한 소속 팀. 콘솔이 본인 키 정보에서 읽어 채우고 직원 함수에는
-- 이 값만 넘긴다.
ALTER TABLE mint.console_sessions
    ADD COLUMN team_ids jsonb NOT NULL DEFAULT '[]'
    CHECK (jsonb_typeof(team_ids) = 'array');
