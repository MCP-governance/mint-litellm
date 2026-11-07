-- 재승인 계획의 종결 상태 하나를 더한다: 작업 실행기가 확정할 수 없는 계획(해시 차이·계획 교체)을
-- 취소해, 서버가 같은 실패를 되풀이하며 영구히 막히지 않고 운영자가 재승인을 다시 요청할 수 있게 한다.
ALTER TABLE mint.contract_reapproval_plans
    DROP CONSTRAINT contract_reapproval_plans_status_check;
ALTER TABLE mint.contract_reapproval_plans
    ADD CONSTRAINT contract_reapproval_plans_status_check
    CHECK (status IN ('pending', 'completed', 'cancelled'));
