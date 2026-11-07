-- 직원 「내 기록」: 행위자 이름이 사용자 식별자와 같다는 이유만으로 운영자 행위가 보이지 않게,
-- 본인이 주체인 호출 판정과 본인 소유 동의·본인 확인·직원 세션에 딸린 사건만 낸다.
CREATE OR REPLACE FUNCTION mint.staff_list_events(p_limit integer DEFAULT 100)
RETURNS SETOF mint.events
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, mint, pg_temp AS $$
    SELECT e.* FROM mint.events e
    WHERE e.instance_id = mint.staff_instance()
      AND (
        e.subject ->> 'principal_id' = mint.staff_user()
        OR (e.kind IN ('consent_granted', 'consent_revoked')
            AND EXISTS (SELECT 1 FROM mint.consents c
                        WHERE c.instance_id = e.instance_id
                          AND c.id::text = split_part(e.source_key, ':', 2)
                          AND c.human_user_id = mint.staff_user()))
        OR (e.kind IN ('call_approval_approved', 'call_approval_rejected',
                       'call_approval_cancelled')
            AND e.payload ->> 'kind' = 'confirmation'
            AND EXISTS (SELECT 1 FROM mint.self_confirmations s
                        WHERE s.instance_id = e.instance_id
                          AND s.id::text = split_part(e.source_key, ':', 2)
                          AND s.principal_id = mint.staff_user()))
        OR (e.kind IN ('console_session_started', 'console_session_ended')
            AND EXISTS (SELECT 1 FROM mint.console_sessions n
                        WHERE n.instance_id = e.instance_id AND n.subject = 'staff'
                          AND n.id_hash = split_part(e.source_key, ':', 2)
                          AND n.user_id = mint.staff_user())))
    ORDER BY e.inserted_at DESC, e.event_id DESC
    LIMIT least(greatest(p_limit, 1), 1000)
$$;
