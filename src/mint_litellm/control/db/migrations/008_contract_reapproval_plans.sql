-- CLI가 고정한 재승인 계획. sync_server의 pin 단계만 승인본과 함께 완료한다.
CREATE TABLE mint.contract_reapproval_plans (
    id uuid PRIMARY KEY,
    instance_id mint.ident NOT NULL,
    server_id text NOT NULL,
    intake_id uuid NOT NULL,
    requested_by text NOT NULL,
    requested_user_id text NOT NULL,
    plan jsonb NOT NULL CHECK (jsonb_typeof(plan) = 'object'),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed')),
    result jsonb,
    UNIQUE (instance_id, server_id),
    FOREIGN KEY (instance_id, server_id) REFERENCES mint.servers (instance_id, server_id),
    FOREIGN KEY (instance_id, intake_id) REFERENCES mint.intakes (instance_id, id),
    CHECK ((status = 'completed') = (result IS NOT NULL))
);
GRANT SELECT, INSERT, UPDATE ON mint.contract_reapproval_plans TO mint_control;
