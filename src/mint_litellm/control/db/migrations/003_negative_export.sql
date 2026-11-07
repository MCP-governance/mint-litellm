-- 부정 상태 전이가 커밋될 때 부정 상태 내보내기 작업을 같은 트랜잭션에 등록한다.
-- 서비스마다 부르지 않고 트리거로 하므로 서비스 계층·수용검증 등 어떤 경로의 전이도 덮는다.
-- 지연 제약 트리거라 트랜잭션이 끝나는 시점에 한 번 걸리고(같은 키는 한 행으로 수렴), 그 시점의
-- 세대를 멱등 키로 쓴다. 롤백된 전이는 작업을 남기지 않는다.

CREATE FUNCTION mint.enqueue_export_negative() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
DECLARE
    ep bigint;
BEGIN
    SELECT epoch INTO ep FROM mint.state_epoch WHERE instance_id = NEW.instance_id;
    INSERT INTO mint.jobs (id, instance_id, kind, target_ref, idempotency_key)
    VALUES (gen_random_uuid(), NEW.instance_id, 'export_negative_state', 'negative_state',
            'export_negative:' || coalesce(ep, 0))
    ON CONFLICT (instance_id, idempotency_key) DO NOTHING;
    RETURN NULL;
END $$;

-- 철회
CREATE CONSTRAINT TRIGGER approvals_zz_export AFTER UPDATE ON mint.approvals
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    WHEN (NEW.status = 'revoked' AND OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION mint.enqueue_export_negative();
-- 거부(새 거부 생성)
CREATE CONSTRAINT TRIGGER denials_zz_export AFTER INSERT ON mint.denials
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    WHEN (NEW.lifted_at IS NULL)
    EXECUTE FUNCTION mint.enqueue_export_negative();
-- 서버 중지·종료·rejected
CREATE CONSTRAINT TRIGGER servers_zz_export AFTER UPDATE ON mint.servers
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    WHEN (NEW.status IN ('rejected', 'suspended', 'terminating', 'terminated')
          AND OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION mint.enqueue_export_negative();
-- 종료 관계
CREATE CONSTRAINT TRIGGER usage_relations_zz_export AFTER UPDATE ON mint.usage_relations
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    WHEN (NEW.status IN ('terminating', 'terminated') AND OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION mint.enqueue_export_negative();
-- 닫힌 조직
CREATE CONSTRAINT TRIGGER orgs_zz_export AFTER UPDATE ON mint.orgs
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    WHEN (NEW.status = 'closed' AND OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION mint.enqueue_export_negative();
-- 해제된 허용 목록
CREATE CONSTRAINT TRIGGER allowlists_zz_export AFTER UPDATE ON mint.allowlists
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    WHEN (NEW.status = 'lifted' AND OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION mint.enqueue_export_negative();
-- 철회된 동의
CREATE CONSTRAINT TRIGGER consents_zz_export AFTER UPDATE ON mint.consents
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
    WHEN (OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL)
    EXECUTE FUNCTION mint.enqueue_export_negative();
