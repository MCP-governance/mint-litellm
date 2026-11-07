-- 직원 세션이 로그인 때 확인한 소속 팀. 콘솔이 본인 키 정보에서 읽어 채우고 직원 함수에는
-- 이 값만 넘긴다.
ALTER TABLE mint.console_sessions
    ADD COLUMN team_ids jsonb NOT NULL DEFAULT '[]'
    CHECK (jsonb_typeof(team_ids) = 'array');

-- ---------------------------------------------------------------------------
-- 직원 함수의 소유 역할. 슈퍼유저가 아니고 로그인하지 못하며, 직원 함수가 쓰는 표와 열의
-- 권한만 가진다. 직원 함수는 SECURITY DEFINER이므로 이 역할의 권한이 곧 직원 화면이 DB에서
-- 할 수 있는 일의 상한이다.
-- 본인 확인 전이 트리거는 현재 역할이 직원 그룹의 구성원인지로 허용 전이를 정하므로 이
-- 역할은 직원 그룹(mint_staff)의 구성원이다. 제어·플러그인·작업 그룹의 구성원이 아니다.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mint_staff_owner') THEN
        CREATE ROLE mint_staff_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOREPLICATION NOBYPASSRLS;
    END IF;
END $$;

GRANT mint_staff TO mint_staff_owner;

-- 읽기 전용 표. FOR SHARE 잠금은 표의 열 하나에 UPDATE 권한을 요구하므로(PostgreSQL 규칙)
-- 낙관적 잠금 버전 열 하나만 열어 둔다. 이 열은 갱신 트리거가 항상 이전 값 + 1로 덮어쓴다.
GRANT SELECT ON mint.orgs, mint.org_bindings, mint.servers, mint.usage_relations, mint.settings
    TO mint_staff_owner;
GRANT UPDATE (state_version) ON
    mint.orgs, mint.org_bindings, mint.servers, mint.usage_relations TO mint_staff_owner;
GRANT UPDATE (settings_version) ON mint.settings TO mint_staff_owner;

-- 직원 세션
GRANT SELECT ON mint.console_sessions TO mint_staff_owner;
GRANT INSERT (id_hash, instance_id, subject, user_id, key_token, role, idle_expiry,
              absolute_expiry, team_ids) ON mint.console_sessions TO mint_staff_owner;
GRANT UPDATE (last_seen_at, idle_expiry, revoked_at) ON mint.console_sessions
    TO mint_staff_owner;

-- 본인 확인: 결정 열과 상태만 바꾼다(요청 열은 갱신 트리거가 고정한다).
GRANT SELECT ON mint.self_confirmations TO mint_staff_owner;
GRANT UPDATE (status, confirmed_by_user_id, confirmed_by_key_token, confirmed_at)
    ON mint.self_confirmations TO mint_staff_owner;

-- 본인 동의
GRANT SELECT ON mint.consents TO mint_staff_owner;
GRANT INSERT (id, instance_id, org_id, human_user_id, server_id, purpose, data_scope,
              text_version, source, granted_at, valid_until, registered_by)
    ON mint.consents TO mint_staff_owner;
GRANT UPDATE (revoked_at) ON mint.consents TO mint_staff_owner;

-- 원장: 추가와 본인 범위 조회
GRANT SELECT ON mint.events TO mint_staff_owner;
GRANT INSERT (event_id, instance_id, kind, source_key, actor, payload)
    ON mint.events TO mint_staff_owner;

-- 행 수준 보안이 켜진 두 표는 이 역할의 정책을 따로 둔다: 요청이 건 배치와 본인의 행만.
CREATE POLICY self_confirmations_staff ON mint.self_confirmations TO mint_staff_owner
    USING (instance_id = nullif(current_setting('mint.staff_instance_id', true), '')
           AND principal_id = nullif(current_setting('mint.staff_user_id', true), ''))
    WITH CHECK (instance_id = nullif(current_setting('mint.staff_instance_id', true), '')
                AND principal_id = nullif(current_setting('mint.staff_user_id', true), ''));

CREATE POLICY console_sessions_staff ON mint.console_sessions TO mint_staff_owner
    USING (subject = 'staff'
           AND instance_id = nullif(current_setting('mint.staff_instance_id', true), '')
           AND user_id = nullif(current_setting('mint.staff_user_id', true), ''))
    WITH CHECK (subject = 'staff'
                AND instance_id = nullif(current_setting('mint.staff_instance_id', true), '')
                AND user_id = nullif(current_setting('mint.staff_user_id', true), ''));

-- 세션 시작은 소속 팀을 같은 문장에서 함께 적는다(팀 없이 만들어진 세션이 남지 않는다).
DROP FUNCTION mint.staff_start_session(text, text, timestamptz, timestamptz);
CREATE FUNCTION mint.staff_start_session(
    p_id_hash text, p_key_token text, p_idle_expiry timestamptz, p_absolute_expiry timestamptz,
    p_team_ids jsonb
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, mint, pg_temp AS $$
DECLARE uid text := mint.staff_user();
BEGIN
    IF p_idle_expiry IS NULL OR p_absolute_expiry IS NULL
       OR p_idle_expiry <= clock_timestamp() OR p_idle_expiry > p_absolute_expiry THEN
        RAISE EXCEPTION 'invalid session expiry' USING ERRCODE = 'check_violation';
    END IF;
    IF p_team_ids IS NULL OR jsonb_typeof(p_team_ids) <> 'array'
       OR EXISTS (SELECT FROM jsonb_array_elements(p_team_ids) v WHERE jsonb_typeof(v) <> 'string')
    THEN
        RAISE EXCEPTION 'invalid team list' USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO mint.console_sessions (id_hash, instance_id, subject, user_id, key_token, role,
                                       idle_expiry, absolute_expiry, team_ids)
    VALUES (p_id_hash, mint.staff_instance(), 'staff', uid, p_key_token, 'staff',
            p_idle_expiry, p_absolute_expiry, p_team_ids);
    PERFORM mint.staff_event('console_session_started', 'console_session', p_id_hash, 1, uid,
                             jsonb_build_object('subject', 'staff'));
END $$;
REVOKE ALL ON FUNCTION mint.staff_start_session(text, text, timestamptz, timestamptz, jsonb)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mint.staff_start_session(text, text, timestamptz, timestamptz, jsonb)
    TO mint_staff;

-- 직원 함수(보조 함수 포함)의 소유자를 바꾼다. 이름이 staff_로 시작하는 mint 함수 전부이다.
-- 슈퍼유저가 아닌 마이그레이션 실행자는 새 소유자가 스키마의 CREATE 권한을 가질 때만 소유자를
-- 바꿀 수 있으므로 바꾸는 동안만 그 권한을 준다. 같은 이유로 실행자가 이 역할로 전환할 수 있어야
-- 하며(역할을 만든 실행자는 관리 옵션만 가진다), 끝나면 전환 옵션을 거둔다.
GRANT CREATE ON SCHEMA mint TO mint_staff_owner;
DO $$
BEGIN
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
        EXECUTE format('GRANT mint_staff_owner TO %I WITH SET TRUE', current_user);
    END IF;
END $$;
DO $$
DECLARE f regprocedure;
BEGIN
    FOR f IN SELECT p.oid::regprocedure FROM pg_proc p
             JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'mint' AND p.proname LIKE 'staff\_%' LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO mint_staff_owner', f);
    END LOOP;
END $$;
REVOKE CREATE ON SCHEMA mint FROM mint_staff_owner;
DO $$
BEGIN
    IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
        EXECUTE format('REVOKE SET OPTION FOR mint_staff_owner FROM %I', current_user);
    END IF;
END $$;
