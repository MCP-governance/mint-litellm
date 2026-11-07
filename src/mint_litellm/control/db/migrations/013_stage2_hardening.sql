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
