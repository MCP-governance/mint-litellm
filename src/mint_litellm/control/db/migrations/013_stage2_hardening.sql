-- 2단계 보강: 복원 요청의 외부 기준 결속과 거절 상태, 되돌림 판정의 행 내용 대조.

-- 복원 요청이 만들어진 시점의 latest.json 해시(파일이 없었으면 'absent'). 게시 때 같은 값일 때만
-- 게시한다. 복원 식별자는 요청 id이고, 게시가 끝나면 restored/<id>/.done이 남는다.
ALTER TABLE mint.restore_requests ADD COLUMN bound_latest text;
ALTER TABLE mint.restore_requests DROP CONSTRAINT restore_requests_status_check;
ALTER TABLE mint.restore_requests
    ADD CONSTRAINT restore_requests_status_check
    CHECK (status IN ('pending', 'published', 'rejected'));

DROP TRIGGER restore_requests_1_status ON mint.restore_requests;
CREATE TRIGGER restore_requests_1_status BEFORE INSERT OR UPDATE ON mint.restore_requests
    FOR EACH ROW EXECUTE FUNCTION mint.check_status(
        'pending', 'pending>published,pending>rejected');
DROP TRIGGER restore_requests_2_frozen ON mint.restore_requests;
CREATE TRIGGER restore_requests_2_frozen BEFORE UPDATE ON mint.restore_requests
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'kind', 'reason', 'requested_by', 'second_approver', 'file_epoch',
        'target_epoch', 'cause', 'created_at', 'bound_latest');

-- 행 내용 대조용 JSON: 세션 시간대와 무관하게 UTC 문자열로 만든다(내보내기 파일의 행 스냅숏과
-- 같은 표기). 플러그인·작업 실행기·명령줄이 같은 함수를 쓴다.
CREATE FUNCTION mint.row_json(r anyelement) RETURNS jsonb
LANGUAGE sql STABLE SET TimeZone = 'UTC' AS $$ SELECT to_jsonb(r) $$;

-- 판정(플러그인 포함)이 복원 진행 중 여부를 읽는다: 복원이 진행 중일 때 파일이 없는 것은 첫 설치가
-- 아니다.
GRANT SELECT ON mint.restore_requests TO mint_plugin;

-- 직원 쓰기는 세션의 마지막 성공 확인이 확인 기한(콘솔이 설정, 기본 10분) 안일 때만 한다.
-- 작업 실행기가 멈춰도 만료가 집행된다.
CREATE OR REPLACE FUNCTION mint.staff_active_session() RETURNS mint.console_sessions
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, mint, pg_temp AS $$
DECLARE
    s mint.console_sessions;
    sid text := nullif(current_setting('mint.staff_session_id_hash', true), '');
    window_seconds integer := coalesce(
        nullif(current_setting('mint.staff_verify_window_seconds', true), '')::integer, 600);
BEGIN
    IF sid IS NULL THEN
        RAISE EXCEPTION 'staff session is not set' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT * INTO s FROM mint.console_sessions
    WHERE id_hash = sid AND instance_id = mint.staff_instance() AND user_id = mint.staff_user()
      AND subject = 'staff' AND role = 'staff' AND revoked_at IS NULL
      AND idle_expiry > clock_timestamp() AND absolute_expiry > clock_timestamp()
      AND last_verified_at > clock_timestamp() - make_interval(secs => window_seconds)
    FOR SHARE;
    IF NOT FOUND OR s.idle_expiry <= clock_timestamp()
       OR s.absolute_expiry <= clock_timestamp() THEN
        RAISE EXCEPTION 'no active staff session' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN s;
END $$;

-- 행 내용 대조 함수는 판정을 하는 역할만 실행한다(직원 로그인은 공개 직원 함수만 실행한다).
REVOKE ALL ON FUNCTION mint.row_json(anyelement) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mint.row_json(anyelement) TO mint_plugin, mint_control, mint_worker;
