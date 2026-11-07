-- 복원 요청. 복원 명령은 DB에서 요청과 부정 상태 재적용을 커밋하고, 파일 게시는 작업 실행기의
-- 작업 restore_publish가 한다(외부 쓰기는 작업 실행기만 한다).
CREATE TABLE mint.restore_requests (
    id           uuid PRIMARY KEY,
    instance_id  mint.ident NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('attest', 'stale_accepted')),
    reason       text NOT NULL CHECK (length(reason) > 0),
    requested_by text NOT NULL,
    second_approver text,
    file_epoch   bigint,
    target_epoch bigint NOT NULL CHECK (target_epoch >= 0),
    cause        text,
    status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'published')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    CHECK ((status = 'published') = (published_at IS NOT NULL))
);

CREATE TRIGGER restore_requests_1_status BEFORE INSERT OR UPDATE ON mint.restore_requests
    FOR EACH ROW EXECUTE FUNCTION mint.check_status('pending', 'pending>published');
CREATE TRIGGER restore_requests_2_frozen BEFORE UPDATE ON mint.restore_requests
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'kind', 'reason', 'requested_by', 'second_approver', 'file_epoch',
        'target_epoch', 'cause', 'created_at');

GRANT SELECT, INSERT ON mint.restore_requests TO mint_control;
GRANT SELECT, UPDATE (status, published_at) ON mint.restore_requests TO mint_worker;
