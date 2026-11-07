-- 관측 원본과 변경 순번. 승인 계약과 분리하여 관측만으로 승인본을 바꾸지 않는다.
CREATE TABLE mint.contract_observations (
    instance_id mint.ident NOT NULL,
    server_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('tool', 'deployment')),
    scope text NOT NULL,
    name text NOT NULL,
    entity_id uuid NOT NULL,
    observed_sha256 text NOT NULL,
    canonical_text text NOT NULL,
    change_seq bigint NOT NULL CHECK (change_seq >= 0),
    observed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instance_id, server_id, kind, scope, name),
    FOREIGN KEY (instance_id, server_id) REFERENCES mint.servers (instance_id, server_id)
);
CREATE TABLE mint.contract_observation_receipts (
    instance_id mint.ident NOT NULL,
    event_id uuid NOT NULL,
    PRIMARY KEY (instance_id, event_id)
);
GRANT SELECT, INSERT, UPDATE ON mint.contract_observations TO mint_control;
GRANT SELECT, INSERT ON mint.contract_observation_receipts TO mint_control;
GRANT SELECT (version) ON mint.tool_contracts TO mint_plugin;
GRANT INSERT ON mint.alerts, mint.litellm_grants TO mint_control;
