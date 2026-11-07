-- 콘솔의 선확인은 화면 오류용이며, 쓰기 트랜잭션이 권한을 다시 확인한다.
-- user/instance/session/team 설정은 인증된 콘솔만 제공한다.
CREATE FUNCTION mint.staff_active_session() RETURNS mint.console_sessions
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, mint, pg_temp AS $$
DECLARE
    s mint.console_sessions;
    sid text := nullif(current_setting('mint.staff_session_id_hash', true), '');
BEGIN
    IF sid IS NULL THEN
        RAISE EXCEPTION 'staff session is not set' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT * INTO s FROM mint.console_sessions
    WHERE id_hash = sid AND instance_id = mint.staff_instance() AND user_id = mint.staff_user()
      AND subject = 'staff' AND role = 'staff' AND revoked_at IS NULL
      AND idle_expiry > clock_timestamp() AND absolute_expiry > clock_timestamp()
    FOR SHARE;
    IF NOT FOUND OR s.idle_expiry <= clock_timestamp()
       OR s.absolute_expiry <= clock_timestamp() THEN
        RAISE EXCEPTION 'no active staff session' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN s;
END $$;

CREATE FUNCTION mint.staff_check_target(p_org_id text, p_server_id text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, mint, pg_temp AS $$
DECLARE
    uid text := mint.staff_user();
    iid text := mint.staff_instance();
    teams jsonb := nullif(current_setting('mint.staff_team_ids', true), '')::jsonb;
    member_orgs text[];
BEGIN
    PERFORM mint.staff_active_session();
    IF teams IS NULL OR jsonb_typeof(teams) <> 'array'
       OR EXISTS (SELECT FROM jsonb_array_elements(teams) v WHERE jsonb_typeof(v) <> 'string') THEN
        RAISE EXCEPTION 'verified staff teams are not set'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM 1 FROM mint.orgs WHERE instance_id = iid AND org_id = p_org_id
        AND status = 'active' FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no active organization' USING ERRCODE = 'insufficient_privilege';
    END IF;
    -- 사용자 바인딩이 있으면 팀·기본 조직으로 넓히지 않는다.
    SELECT array_agg(b.org_id) INTO member_orgs FROM (
        SELECT org_id FROM mint.org_bindings
        WHERE instance_id = iid AND subject_type = 'user' AND subject_id = uid FOR SHARE
    ) b;
    IF member_orgs IS NULL THEN
        SELECT array_agg(b.org_id) INTO member_orgs FROM (
            SELECT org_id FROM mint.org_bindings WHERE instance_id = iid AND subject_type = 'team'
              AND subject_id IN (SELECT jsonb_array_elements_text(teams)) FOR SHARE
        ) b;
    END IF;
    IF member_orgs IS NULL THEN
        SELECT ARRAY[default_org_id::text] INTO member_orgs FROM mint.settings
        WHERE instance_id = iid FOR SHARE;
    END IF;
    IF coalesce(p_org_id = ANY(member_orgs), false) IS NOT TRUE THEN
        RAISE EXCEPTION 'not a member of the organization' USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM 1 FROM mint.servers WHERE instance_id = iid AND server_id = p_server_id
        AND status = 'approved' FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'server is not approved' USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM 1 FROM mint.usage_relations WHERE instance_id = iid AND org_id = p_org_id
        AND server_id = p_server_id AND status = 'active' FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no active usage relation' USING ERRCODE = 'insufficient_privilege';
    END IF;
END $$;

CREATE OR REPLACE FUNCTION mint.staff_decide_confirmation(
    p_id uuid, p_target text, p_key_token text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, mint, pg_temp AS $$
DECLARE
    uid text := mint.staff_user();
    sc mint.self_confirmations;
    s mint.console_sessions := mint.staff_active_session();
    separate_key boolean;
BEGIN
    SELECT * INTO sc FROM mint.self_confirmations
    WHERE id = p_id AND principal_id = uid AND instance_id = mint.staff_instance() FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no such self confirmation for this user'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM mint.staff_check_target(sc.org_id, sc.server_id);
    IF p_target NOT IN ('confirmed', 'rejected', 'cancelled') OR p_target IS NULL
       OR (p_target IN ('confirmed', 'rejected') AND sc.status <> 'pending')
       OR (p_target = 'cancelled' AND sc.status NOT IN ('pending', 'confirmed')) THEN
        RAISE EXCEPTION 'confirmation already decided' USING ERRCODE = 'check_violation';
    END IF;
    IF p_target IN ('confirmed', 'rejected') THEN
        IF sc.expires_at <= clock_timestamp() THEN
            RAISE EXCEPTION 'this self confirmation has expired' USING ERRCODE = 'check_violation';
        END IF;
        IF p_key_token IS DISTINCT FROM s.key_token THEN
            RAISE EXCEPTION 'key does not match staff session'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        SELECT require_separate_confirm_key INTO separate_key FROM mint.settings
        WHERE instance_id = mint.staff_instance() FOR SHARE;
        IF p_target = 'confirmed' AND coalesce(separate_key, true)
           AND s.key_token = sc.requested_by_key_token THEN
            RAISE EXCEPTION 'confirmation requires another key'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        UPDATE mint.self_confirmations
        SET status = p_target, confirmed_by_user_id = uid,
            confirmed_by_key_token = s.key_token, confirmed_at = clock_timestamp()
        WHERE id = p_id RETURNING * INTO sc;
    ELSE
        UPDATE mint.self_confirmations SET status = p_target WHERE id = p_id RETURNING * INTO sc;
    END IF;
    PERFORM mint.staff_event(
        CASE p_target WHEN 'confirmed' THEN 'call_approval_approved'
                      WHEN 'rejected' THEN 'call_approval_rejected'
                      ELSE 'call_approval_cancelled' END,
        'call_approval', p_id::text, sc.state_version, uid,
        jsonb_build_object('kind', 'confirmation'));
END $$;

CREATE OR REPLACE FUNCTION mint.staff_grant_consent(
    p_id uuid, p_org_id text, p_server_id text, p_purpose text, p_data_scope text,
    p_text_version text, p_valid_until timestamptz
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, mint, pg_temp AS $$
DECLARE
    uid text := mint.staff_user();
    max_days integer := nullif(current_setting('mint.staff_consent_max_days', true), '')::integer;
BEGIN
    PERFORM mint.staff_check_target(p_org_id, p_server_id);
    IF max_days IS NULL OR max_days NOT BETWEEN 1 AND 365 OR p_valid_until IS NULL
       OR p_valid_until <= clock_timestamp()
       OR p_valid_until > clock_timestamp() + make_interval(days => max_days) THEN
        RAISE EXCEPTION 'invalid consent validity' USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO mint.consents (id, instance_id, org_id, human_user_id, server_id, purpose,
                               data_scope, text_version, source, granted_at, valid_until,
                               registered_by)
    VALUES (p_id, mint.staff_instance(), p_org_id, uid, p_server_id, p_purpose, p_data_scope,
            p_text_version, 'self_service', clock_timestamp(), p_valid_until, uid);
    PERFORM mint.staff_event('consent_granted', 'consent', p_id::text, 1, uid, '{}'::jsonb);
END $$;

CREATE OR REPLACE FUNCTION mint.staff_revoke_consent(p_id uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, mint, pg_temp AS $$
DECLARE
    uid text := mint.staff_user();
    c mint.consents;
BEGIN
    PERFORM mint.staff_active_session();
    SELECT * INTO c FROM mint.consents WHERE id = p_id AND human_user_id = uid
      AND revoked_at IS NULL AND instance_id = mint.staff_instance() FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no active consent of this user' USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM mint.staff_check_target(c.org_id, c.server_id);
    UPDATE mint.consents SET revoked_at = clock_timestamp() WHERE id = p_id
    RETURNING * INTO c;
    PERFORM mint.staff_event('consent_revoked', 'consent', p_id::text, c.state_version, uid, '{}');
END $$;

-- 세션 생성은 로그인 경계라 기존 활성 세션을 요구할 수 없다.
-- start는 인증된 콘솔의 user/instance 설정을 믿고, touch/end는 대상 세션 자체를 검사한다.
CREATE OR REPLACE FUNCTION mint.staff_start_session(
    p_id_hash text, p_key_token text, p_idle_expiry timestamptz, p_absolute_expiry timestamptz
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, mint, pg_temp AS $$
DECLARE uid text := mint.staff_user();
BEGIN
    IF p_idle_expiry IS NULL OR p_absolute_expiry IS NULL
       OR p_idle_expiry <= clock_timestamp() OR p_idle_expiry > p_absolute_expiry THEN
        RAISE EXCEPTION 'invalid session expiry' USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO mint.console_sessions (id_hash, instance_id, subject, user_id, key_token, role,
                                       idle_expiry, absolute_expiry)
    VALUES (p_id_hash, mint.staff_instance(), 'staff', uid, p_key_token, 'staff',
            p_idle_expiry, p_absolute_expiry);
    PERFORM mint.staff_event('console_session_started', 'console_session', p_id_hash, 1, uid,
                             jsonb_build_object('subject', 'staff'));
END $$;

CREATE OR REPLACE FUNCTION mint.staff_touch_session(p_id_hash text, p_idle_expiry timestamptz)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, mint, pg_temp AS $$
BEGIN
    PERFORM mint.staff_active_session();
    IF p_id_hash IS DISTINCT FROM current_setting('mint.staff_session_id_hash', true)
       OR p_idle_expiry IS NULL OR p_idle_expiry <= clock_timestamp() THEN
        RAISE EXCEPTION 'invalid session renewal' USING ERRCODE = 'insufficient_privilege';
    END IF;
    UPDATE mint.console_sessions
    SET last_seen_at = clock_timestamp(), idle_expiry = least(p_idle_expiry, absolute_expiry)
    WHERE id_hash = p_id_hash;
END $$;

CREATE OR REPLACE FUNCTION mint.staff_end_session(p_id_hash text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, mint, pg_temp AS $$
BEGIN
    PERFORM mint.staff_active_session();
    IF p_id_hash IS DISTINCT FROM current_setting('mint.staff_session_id_hash', true) THEN
        RAISE EXCEPTION 'session mismatch' USING ERRCODE = 'insufficient_privilege';
    END IF;
    UPDATE mint.console_sessions SET revoked_at = clock_timestamp() WHERE id_hash = p_id_hash;
    PERFORM mint.staff_event('console_session_ended', 'console_session', p_id_hash, 2,
        mint.staff_user(), jsonb_build_object('subject', 'staff'));
END $$;

-- pg_temp를 명시적으로 마지막에 둔다. 기존 함수도 같은 경로로 고정한다.
DO $$
DECLARE f regprocedure;
BEGIN
    FOR f IN SELECT p.oid::regprocedure FROM pg_proc p
             JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'mint' AND p.proname LIKE 'staff\_%' LOOP
        EXECUTE format('ALTER FUNCTION %s SET search_path = pg_catalog, mint, pg_temp', f);
    END LOOP;
END $$;
REVOKE ALL ON FUNCTION mint.staff_active_session() FROM PUBLIC;
REVOKE ALL ON FUNCTION mint.staff_check_target(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION mint.staff_user(), mint.staff_instance() FROM PUBLIC;
