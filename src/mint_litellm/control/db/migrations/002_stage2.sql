-- MINT 2단계 스키마. 001 위에 한 번만 적용한다.
-- 001의 표는 열 추가, 서버 전이 두 개, 서버 승인 보호, 플러그인 원장 INSERT 종류 제한,
-- 세대 올리기 함수, 세대 트리거 대상 조정 말고는 바꾸지 않는다.

-- ---------------------------------------------------------------------------
-- 역할
--   mint_staff:  직원 화면. 표에 직접 쓰지 않고 mint.staff_* 함수만 실행한다.
--   mint_worker: 작업 실행기의 작업·동기화·내보내기·경보 표.
-- 로그인 역할은 프로세스마다 아래 그룹 역할을 받는다.
--   콘솔 관리 화면·CLI: mint_control
--   콘솔 직원 화면:     mint_staff
--   작업 실행기:        mint_control + mint_worker + mint_sealer
--   플러그인:           mint_plugin
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['mint_staff', 'mint_worker', 'mint_sealer'] LOOP
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', r);
        END IF;
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA mint TO mint_staff, mint_worker;

-- 직원 화면 연결은 트랜잭션마다 set_config('mint.staff_user_id', <user_id>, true)와
-- set_config('mint.staff_instance_id', <instance_id>, true)로 본인과 배치를 밝힌다.
-- 직원 함수는 이 값에서만 대상을 읽고, 비어 있으면 거부한다.
CREATE FUNCTION mint.staff_user() RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE
    uid text := nullif(current_setting('mint.staff_user_id', true), '');
BEGIN
    IF uid IS NULL THEN
        RAISE EXCEPTION 'mint.staff_user_id is not set' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN uid;
END $$;

CREATE FUNCTION mint.staff_instance() RETURNS text
LANGUAGE plpgsql STABLE AS $$
DECLARE
    iid text := nullif(current_setting('mint.staff_instance_id', true), '');
BEGIN
    IF iid IS NULL THEN
        RAISE EXCEPTION 'mint.staff_instance_id is not set'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN iid;
END $$;

-- 추가만 가능한 표의 행 수정·삭제를 막는다.
CREATE FUNCTION mint.reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'check_violation';
END $$;

-- ---------------------------------------------------------------------------
-- 1단계 표 열 추가. call_approvals는 바꾸지 않는다.
-- ---------------------------------------------------------------------------

ALTER TABLE mint.servers
    ADD COLUMN contract_status text NOT NULL DEFAULT 'none'
        CHECK (contract_status IN ('none', 'managed')),
    ADD COLUMN issuer text CHECK (issuer IS NULL OR length(issuer) > 0),
    ADD COLUMN last_observed_at timestamptz,
    ADD COLUMN deployment_drift boolean NOT NULL DEFAULT false,
    ADD COLUMN deployment_drift_fields text[];

ALTER TABLE mint.settings
    ADD COLUMN control_key_token text
        CHECK (control_key_token IS NULL OR length(control_key_token) > 0),
    ADD COLUMN require_separate_confirm_key boolean NOT NULL DEFAULT true;

-- ---------------------------------------------------------------------------
-- 신청과 수용검증
-- ---------------------------------------------------------------------------

-- 신청 입력은 받은 직후(received)에는 비어 있을 수 있고 검사로 넘어가려면 모두 있어야 한다.
CREATE TABLE mint.intakes (
    id                         uuid PRIMARY KEY,
    instance_id                mint.ident NOT NULL,
    org_id                     mint.ident NOT NULL,
    server_id                  text NOT NULL,
    source                     text NOT NULL CHECK (source IN ('submission', 'operator')),
    status                     text NOT NULL DEFAULT 'received' CHECK (status IN (
        'received', 'checking', 'passed', 'failed', 'indeterminate', 'decided')),
    purpose                    text CHECK (purpose IS NULL OR length(purpose) BETWEEN 1 AND 256),
    data_grade                 text CHECK (data_grade IS NULL OR data_grade IN (
        'public', 'internal', 'confidential', 'restricted')),
    allowed_resources          jsonb CHECK (allowed_resources IS NULL
                                            OR jsonb_typeof(allowed_resources) IN ('array', 'object')),
    provider                   text CHECK (provider IS NULL OR length(provider) > 0),
    owner_user_id              text CHECK (owner_user_id IS NULL OR length(owner_user_id) > 0),
    readiness                  jsonb CHECK (readiness IS NULL OR (
        jsonb_typeof(readiness) = 'object'
        AND readiness ?& ARRAY['credential_notice', 'revocation_record', 'audit_access_days'])),
    data_return_terms          text CHECK (data_return_terms IS NULL OR length(data_return_terms) > 0),
    requested_by               text CHECK (requested_by IS NULL OR length(requested_by) > 0),
    decided_by                 text,
    decision                   text CHECK (decision IN ('approved', 'rejected', 'exception')),
    decision_reason            text,
    needs_registry_credentials boolean NOT NULL DEFAULT false,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    decided_at                 timestamptz,
    state_version              bigint NOT NULL DEFAULT 1,
    FOREIGN KEY (instance_id, org_id) REFERENCES mint.orgs (instance_id, org_id),
    FOREIGN KEY (instance_id, server_id) REFERENCES mint.servers (instance_id, server_id),
    UNIQUE (instance_id, id),
    CHECK ((status = 'decided') = (decision IS NOT NULL)),
    CHECK ((decision IS NULL) = (decided_by IS NULL) AND (decision IS NULL) = (decided_at IS NULL)),
    CHECK (decision IS DISTINCT FROM 'exception'
           OR (decision_reason IS NOT NULL AND length(decision_reason) > 0)),
    CHECK (status = 'received' OR (
        purpose IS NOT NULL AND data_grade IS NOT NULL AND allowed_resources IS NOT NULL
        AND provider IS NOT NULL AND owner_user_id IS NOT NULL AND readiness IS NOT NULL
        AND data_return_terms IS NOT NULL AND requested_by IS NOT NULL))
);

-- 서버마다 결정되지 않은 신청은 하나뿐이다.
CREATE UNIQUE INDEX intakes_one_open_per_server
    ON mint.intakes (instance_id, server_id) WHERE status <> 'decided';

CREATE TABLE mint.intake_checks (
    id                   uuid PRIMARY KEY,
    instance_id          mint.ident NOT NULL,
    intake_id            uuid NOT NULL,
    "check"              text NOT NULL CHECK ("check" IN (
        'url_guard', 'oauth_issuer_allowlist', 'connection', 'definitions',
        'description_scan', 'risk_grade', 'termination_readiness', 'supply_chain')),
    attempt              integer NOT NULL CHECK (attempt >= 1),
    result               text NOT NULL CHECK (result IN ('pass', 'fail', 'indeterminate')),
    reason_code          text CHECK (reason_code IS NULL OR length(reason_code) > 0),
    method               text NOT NULL CHECK (length(method) > 0),
    inputs               jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(inputs) = 'object'),
    evidence_ref         text,
    evidence_sha256      text CHECK (evidence_sha256 IS NULL OR evidence_sha256 ~ '^[0-9a-f]{64}$'),
    evidence_valid_until timestamptz,
    ran_at               timestamptz NOT NULL DEFAULT now(),
    schema_version       integer NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    FOREIGN KEY (instance_id, intake_id) REFERENCES mint.intakes (instance_id, id),
    UNIQUE (intake_id, "check", attempt)
);

-- ---------------------------------------------------------------------------
-- 계약
-- ---------------------------------------------------------------------------

-- 위험 등급과 행위 유형은 도구 정의를 모은 직후에는 비어 있고 승인 전에 채워진다.
CREATE TABLE mint.tool_contracts (
    id               uuid PRIMARY KEY,
    instance_id      mint.ident NOT NULL,
    server_id        text NOT NULL,
    credential_scope text NOT NULL CHECK (credential_scope IN ('server', 'per_user')),
    tool             text NOT NULL CHECK (length(tool) > 0),
    canonical_text   text NOT NULL,
    sha256           text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    pin_sha256       text NOT NULL CHECK (pin_sha256 ~ '^[0-9a-f]{64}$'),
    risk_grade       text CHECK (risk_grade IN ('low', 'medium', 'high', 'critical')),
    action_type      text CHECK (action_type IN (
        'read', 'write', 'delete', 'external_send', 'credential_access')),
    source           text NOT NULL CHECK (source IN ('intake', 'pin', 'observed')),
    status           text NOT NULL DEFAULT 'candidate'
                     CHECK (status IN ('candidate', 'approved', 'superseded')),
    drift            text NOT NULL DEFAULT 'none' CHECK (drift IN (
        'none', 'input_schema', 'description', 'output_schema', 'removed')),
    observed_sha256  text CHECK (observed_sha256 IS NULL OR observed_sha256 ~ '^[0-9a-f]{64}$'),
    observed_at      timestamptz,
    version          bigint NOT NULL DEFAULT 1,
    FOREIGN KEY (instance_id, server_id) REFERENCES mint.servers (instance_id, server_id),
    CHECK ((observed_sha256 IS NULL) = (observed_at IS NULL)),
    CHECK (status <> 'approved' OR (risk_grade IS NOT NULL AND action_type IS NOT NULL))
);

CREATE UNIQUE INDEX tool_contracts_one_approved
    ON mint.tool_contracts (instance_id, server_id, credential_scope, tool)
    WHERE status = 'approved';

CREATE INDEX tool_contracts_by_server ON mint.tool_contracts (instance_id, server_id);

CREATE TABLE mint.deployment_contracts (
    id             uuid PRIMARY KEY,
    instance_id    mint.ident NOT NULL,
    server_id      text NOT NULL,
    canonical_text text NOT NULL,
    sha256         text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    url            text NOT NULL CHECK (length(url) > 0),
    url_sha256     text NOT NULL CHECK (url_sha256 ~ '^[0-9a-f]{64}$'),
    transport      text NOT NULL CHECK (length(transport) > 0),
    auth_type      text NOT NULL CHECK (length(auth_type) > 0),
    issuer         text CHECK (issuer IS NULL OR length(issuer) > 0),
    oauth_scopes   text[] NOT NULL DEFAULT '{}',
    allowed_tools  text[],
    image_digest   text,
    source_commit  text,
    status         text NOT NULL DEFAULT 'candidate'
                   CHECK (status IN ('candidate', 'approved', 'superseded')),
    version        bigint NOT NULL DEFAULT 1,
    FOREIGN KEY (instance_id, server_id) REFERENCES mint.servers (instance_id, server_id)
);

CREATE UNIQUE INDEX deployment_contracts_one_approved
    ON mint.deployment_contracts (instance_id, server_id) WHERE status = 'approved';

-- ---------------------------------------------------------------------------
-- 작업 실행기
-- ---------------------------------------------------------------------------

CREATE TABLE mint.jobs (
    id              uuid PRIMARY KEY,
    instance_id     mint.ident NOT NULL,
    kind            text NOT NULL CHECK (kind ~ '^[a-z][a-z_]*$'),
    target_ref      text NOT NULL CHECK (length(target_ref) BETWEEN 1 AND 512),
    idempotency_key text CHECK (idempotency_key IS NULL OR length(idempotency_key) BETWEEN 1 AND 512),
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'running', 'done', 'failed')),
    attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts    integer NOT NULL DEFAULT 8 CHECK (max_attempts >= 1),
    run_after       timestamptz NOT NULL DEFAULT now(),
    lease_owner     text,
    lease_until     timestamptz,
    last_error_code text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (instance_id, idempotency_key),
    CHECK (status <> 'running' OR (lease_owner IS NOT NULL AND lease_until IS NOT NULL))
);

-- 수렴 작업(멱등 키 없음)은 대상마다 대기 행이 하나뿐이다.
CREATE UNIQUE INDEX jobs_one_pending_convergent
    ON mint.jobs (instance_id, kind, target_ref)
    WHERE status = 'pending' AND idempotency_key IS NULL;

CREATE INDEX jobs_ready ON mint.jobs (instance_id, run_after) WHERE status = 'pending';
CREATE INDEX jobs_leased ON mint.jobs (instance_id, lease_until) WHERE status = 'running';

CREATE TABLE mint.schedules (
    instance_id      mint.ident NOT NULL,
    kind             text NOT NULL CHECK (kind ~ '^[a-z][a-z_]*$'),
    interval_seconds integer NOT NULL CHECK (interval_seconds >= 1),
    next_run_at      timestamptz NOT NULL,
    enabled          boolean NOT NULL DEFAULT true,
    PRIMARY KEY (instance_id, kind)
);

-- 대상 하나·항목 하나·서버 하나의 LiteLLM 반영 상태. 서버 대상 항목(승인·고정)은 서버를 대상으로 한다.
CREATE TABLE mint.litellm_grants (
    id                   uuid PRIMARY KEY,
    instance_id          mint.ident NOT NULL,
    target_type          text NOT NULL CHECK (target_type IN ('key', 'team', 'server')),
    target_id            text NOT NULL CHECK (length(target_id) > 0),
    item                 text NOT NULL CHECK (item IN (
        'mcp_server', 'tool_permission', 'server_approval', 'pin')),
    server_id            text NOT NULL,
    tools                text[],
    preexisting          boolean NOT NULL DEFAULT false,
    applied_sha256       text CHECK (applied_sha256 IS NULL OR applied_sha256 ~ '^[0-9a-f]{64}$'),
    observed             boolean,
    observed_at          timestamptz,
    missing_count        integer NOT NULL DEFAULT 0 CHECK (missing_count >= 0),
    missing_window_start timestamptz,
    held                 boolean NOT NULL DEFAULT false,
    held_at              timestamptz,
    last_applied_at      timestamptz,
    state_version        bigint NOT NULL DEFAULT 1,
    FOREIGN KEY (instance_id, server_id) REFERENCES mint.servers (instance_id, server_id),
    UNIQUE (instance_id, target_type, target_id, item, server_id),
    CHECK ((target_type = 'server') = (item IN ('server_approval', 'pin'))),
    CHECK (item = 'pin' OR applied_sha256 IS NULL)
);

-- ---------------------------------------------------------------------------
-- 봉인·내보내기·경보
-- ---------------------------------------------------------------------------

CREATE TABLE mint.seal_batches (
    instance_id    mint.ident NOT NULL,
    from_seal_seq  bigint NOT NULL CHECK (from_seal_seq >= 1),
    to_seal_seq    bigint NOT NULL,
    prev_tail_hash bytea NOT NULL CHECK (length(prev_tail_hash) = 32),
    tail_hash      bytea NOT NULL CHECK (length(tail_hash) = 32),
    sealed_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instance_id, to_seal_seq),
    CHECK (from_seal_seq <= to_seal_seq)
);

CREATE TABLE mint.exports (
    id             uuid PRIMARY KEY,
    instance_id    mint.ident NOT NULL,
    kind           text NOT NULL CHECK (kind IN ('negative_state', 'checkpoint')),
    state_epoch    bigint NOT NULL CHECK (state_epoch >= 0),
    to_seal_seq    bigint CHECK (to_seal_seq IS NULL OR to_seal_seq >= 1),
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    path           text NOT NULL CHECK (length(path) > 0),
    status         text NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'written', 'failed')),
    attempts       integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    written_at     timestamptz,
    CHECK ((status = 'written') = (written_at IS NOT NULL))
);

-- 부정 상태 파일은 세대마다 하나다.
CREATE UNIQUE INDEX exports_negative_per_epoch
    ON mint.exports (instance_id, kind, state_epoch) WHERE kind = 'negative_state';

CREATE TABLE mint.alerts (
    id              uuid PRIMARY KEY,
    instance_id     mint.ident NOT NULL,
    kind            text NOT NULL CHECK (kind ~ '^[a-z][a-z_]*$'),
    severity        text NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    dedup_key       text NOT NULL CHECK (length(dedup_key) BETWEEN 1 AND 512),
    subject         jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(subject) = 'object'),
    first_at        timestamptz NOT NULL DEFAULT now(),
    last_at         timestamptz NOT NULL DEFAULT now(),
    count           integer NOT NULL DEFAULT 1 CHECK (count >= 1),
    status          text NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'acknowledged', 'resolved')),
    acknowledged_by text,
    resolved_at     timestamptz,
    UNIQUE (instance_id, id),
    CHECK (status <> 'acknowledged' OR acknowledged_by IS NOT NULL),
    CHECK ((status = 'resolved') = (resolved_at IS NOT NULL))
);

-- 해결되지 않은 경보는 같은 키로 하나뿐이다.
CREATE UNIQUE INDEX alerts_one_unresolved
    ON mint.alerts (instance_id, dedup_key) WHERE status <> 'resolved';

CREATE TABLE mint.alert_deliveries (
    id          uuid PRIMARY KEY,
    instance_id mint.ident NOT NULL,
    alert_id    uuid NOT NULL,
    channel     text NOT NULL CHECK (length(channel) > 0),
    delivery_id uuid NOT NULL,
    status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    attempts    integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    sent_at     timestamptz,
    FOREIGN KEY (instance_id, alert_id) REFERENCES mint.alerts (instance_id, id),
    UNIQUE (instance_id, delivery_id),
    CHECK ((status = 'sent') = (sent_at IS NOT NULL))
);

-- ---------------------------------------------------------------------------
-- 콘솔 세션
-- ---------------------------------------------------------------------------

CREATE TABLE mint.console_sessions (
    id_hash          text PRIMARY KEY CHECK (id_hash ~ '^[0-9a-f]{64}$'),
    instance_id      mint.ident NOT NULL,
    subject          text NOT NULL CHECK (subject IN ('admin', 'staff')),
    user_id          text NOT NULL CHECK (length(user_id) > 0),
    key_token        text NOT NULL CHECK (length(key_token) > 0),
    operator_name    text CHECK (operator_name IS NULL OR length(operator_name) > 0),
    role             text NOT NULL CHECK (role IN ('admin', 'approver', 'viewer', 'staff')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at     timestamptz NOT NULL DEFAULT now(),
    last_verified_at timestamptz NOT NULL DEFAULT now(),
    idle_expiry      timestamptz NOT NULL,
    absolute_expiry  timestamptz NOT NULL,
    revoked_at       timestamptz,
    CHECK (subject <> 'admin' OR operator_name IS NOT NULL),
    CHECK (idle_expiry <= absolute_expiry)
);

CREATE INDEX console_sessions_by_user ON mint.console_sessions (instance_id, user_id);

-- ---------------------------------------------------------------------------
-- 본인 확인. 호출별 승인(call_approvals)과 별도 표이고 소비도 따로 한다.
-- ---------------------------------------------------------------------------

CREATE TABLE mint.self_confirmations (
    id                      uuid PRIMARY KEY,
    instance_id             mint.ident NOT NULL,
    org_id                  mint.ident NOT NULL,
    principal_id            text NOT NULL CHECK (length(principal_id) > 0),
    server_id               text NOT NULL,
    tool                    text NOT NULL CHECK (length(tool) > 0),
    args_fp                 text NOT NULL CHECK (args_fp ~ '^[0-9a-f]{64}$'),
    fp_key_version          integer NOT NULL CHECK (fp_key_version >= 1),
    policy_version_id       mint.ident NOT NULL,
    purpose                 text NOT NULL CHECK (length(purpose) BETWEEN 1 AND 128),
    status                  text NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'confirmed', 'rejected', 'cancelled', 'expired', 'consumed')),
    requested_by_key_token  text NOT NULL CHECK (length(requested_by_key_token) > 0),
    confirmed_by_user_id    text CHECK (confirmed_by_user_id IS NULL
                                        OR length(confirmed_by_user_id) > 0),
    confirmed_by_key_token  text CHECK (confirmed_by_key_token IS NULL
                                        OR length(confirmed_by_key_token) > 0),
    confirmed_at            timestamptz,
    display                 jsonb CHECK (display IS NULL OR jsonb_typeof(display) = 'object'),
    expires_at              timestamptz NOT NULL,
    consumed_invocation_id  uuid,
    consumed_at             timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now(),
    state_version           bigint NOT NULL DEFAULT 1,
    FOREIGN KEY (instance_id, org_id) REFERENCES mint.orgs (instance_id, org_id),
    FOREIGN KEY (instance_id, server_id) REFERENCES mint.servers (instance_id, server_id),
    FOREIGN KEY (instance_id, policy_version_id)
        REFERENCES mint.policy_versions (instance_id, id),
    CHECK ((confirmed_by_user_id IS NULL) = (confirmed_by_key_token IS NULL)
           AND (confirmed_by_user_id IS NULL) = (confirmed_at IS NULL)),
    CHECK (status <> 'pending' OR confirmed_by_user_id IS NULL),
    CHECK (status NOT IN ('confirmed', 'consumed', 'rejected') OR confirmed_by_user_id IS NOT NULL),
    CHECK ((status = 'consumed') = (consumed_invocation_id IS NOT NULL)),
    CHECK ((consumed_invocation_id IS NULL) = (consumed_at IS NULL))
);

CREATE UNIQUE INDEX self_confirmations_pending_once
    ON mint.self_confirmations
        (instance_id, principal_id, server_id, tool, args_fp, policy_version_id)
    WHERE status = 'pending';

CREATE UNIQUE INDEX self_confirmations_consumed_once
    ON mint.self_confirmations (instance_id, consumed_invocation_id)
    WHERE consumed_invocation_id IS NOT NULL;

CREATE INDEX self_confirmations_by_principal
    ON mint.self_confirmations (instance_id, principal_id, status);

-- 새 행은 대기 상태이고 결정·소비 열이 비어 있어야 한다.
CREATE FUNCTION mint.check_self_confirmation_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status <> 'pending'
       OR NEW.confirmed_by_user_id IS NOT NULL OR NEW.confirmed_by_key_token IS NOT NULL
       OR NEW.confirmed_at IS NOT NULL
       OR NEW.consumed_invocation_id IS NOT NULL OR NEW.consumed_at IS NOT NULL THEN
        RAISE EXCEPTION 'a self confirmation starts pending with empty decision columns'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

-- 역할마다 할 수 있는 전이를 합친 것이 허용 전이다(슈퍼유저는 모든 역할의 구성원이다).
--   mint_plugin:  confirmed>consumed
--   mint_staff:   pending>confirmed|rejected, pending|confirmed>cancelled (직원 함수가 본인 행에만 건다)
--   mint_control: pending|confirmed>cancelled, pending|confirmed>expired (확인·거절은 불가)
-- 확인·거절의 주체는 요청 주체 본인이어야 한다.
CREATE FUNCTION mint.restrict_self_confirmation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    step text := OLD.status || '>' || NEW.status;
    is_plugin boolean := pg_has_role(current_user, 'mint_plugin', 'MEMBER');
    is_staff boolean := pg_has_role(current_user, 'mint_staff', 'MEMBER');
    is_control boolean := pg_has_role(current_user, 'mint_control', 'MEMBER');
BEGIN
    IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'a self confirmation only changes by a status transition'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT ((is_plugin AND step = 'confirmed>consumed')
            OR (is_staff AND step IN (
                'pending>confirmed', 'pending>rejected', 'pending>cancelled',
                'confirmed>cancelled'))
            OR (is_control AND step IN (
                'pending>cancelled', 'confirmed>cancelled', 'pending>expired',
                'confirmed>expired'))) THEN
        RAISE EXCEPTION 'this role cannot move a self confirmation from % to %',
            OLD.status, NEW.status USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF step IN ('pending>confirmed', 'pending>rejected')
       AND NEW.confirmed_by_user_id IS DISTINCT FROM OLD.principal_id THEN
        RAISE EXCEPTION 'only the requesting principal can confirm or reject'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;

-- 표시 값은 만든 뒤 바꿀 수 없고, 종결 전이에서만 같은 트랜잭션에서 지워진다.
CREATE FUNCTION mint.clear_self_confirmation_display() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status IN ('consumed', 'rejected', 'cancelled', 'expired') THEN
        NEW.display := NULL;
    ELSIF NEW.display IS DISTINCT FROM OLD.display THEN
        RAISE EXCEPTION 'self_confirmations.display cannot be changed'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

-- ---------------------------------------------------------------------------
-- 1단계 트리거 개정
-- ---------------------------------------------------------------------------

-- 서버 전이에 재검사(awaiting_approval>validating)와 거절(awaiting_approval>rejected)을 더한다.
DROP TRIGGER servers_1_status ON mint.servers;
CREATE TRIGGER servers_1_status BEFORE INSERT OR UPDATE ON mint.servers
    FOR EACH ROW EXECUTE FUNCTION mint.check_status(
        'pending,awaiting_approval',
        'pending>validating,validating>awaiting_approval,validating>rejected,'
        'awaiting_approval>approved,awaiting_approval>validating,awaiting_approval>rejected,'
        'approved>suspended,suspended>approved,'
        'approved>terminating,suspended>terminating,terminating>terminated,'
        'rejected>validating');

-- 결정되지 않은 신청이 있는 서버는 수용검증을 거치지 않고 승인할 수 없다.
CREATE FUNCTION mint.guard_server_approval() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM mint.intakes i
        WHERE i.instance_id = NEW.instance_id AND i.server_id = NEW.server_id
          AND i.status <> 'decided'
    ) THEN
        RAISE EXCEPTION 'server % has an intake that is not decided', NEW.server_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER servers_4_intake BEFORE UPDATE ON mint.servers
    FOR EACH ROW WHEN (OLD.status = 'awaiting_approval' AND NEW.status = 'approved')
    EXECUTE FUNCTION mint.guard_server_approval();

-- 판정 프로세스 역할만 가진 접속은 호출 사건과 시스템 사건만 원장에 추가할 수 있다.
-- 제어 평면 역할의 구성원과 슈퍼유저는 제한을 받지 않는다.
CREATE FUNCTION mint.restrict_plugin_events() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF pg_has_role(current_user, 'mint_plugin', 'MEMBER')
       AND NOT pg_has_role(current_user, 'mint_control', 'MEMBER')
       AND NEW.kind NOT IN (
           'decision_evaluated', 'discovery_evaluated', 'call_response_received',
           'call_outcome_unknown',
           'enforcement_unverified', 'enforcement_verified',
           'server_identity_mismatch_observed', 'buffer_overflow', 'buffer_replayed') THEN
        RAISE EXCEPTION 'the plugin role can only append call and system events'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER events_0_plugin_kinds BEFORE INSERT ON mint.events
    FOR EACH ROW EXECUTE FUNCTION mint.restrict_plugin_events();

-- 세대는 복원 확인 절차만 올린다. 현재 세대보다 커야 하고 한 번에 1,000까지만 올리며 내리지 않는다.
-- 직접 실행 권한은 아무에게도 주지 않고 아래 래퍼만 제어 평면 역할에 연다.
CREATE FUNCTION mint.raise_epoch(p_instance_id text, p_target bigint) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
DECLARE
    current_epoch bigint;
BEGIN
    SELECT epoch INTO current_epoch FROM mint.state_epoch
    WHERE instance_id = p_instance_id FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO mint.state_epoch (instance_id, epoch) VALUES (p_instance_id, 0)
        ON CONFLICT (instance_id) DO NOTHING;
        SELECT epoch INTO current_epoch FROM mint.state_epoch
        WHERE instance_id = p_instance_id FOR UPDATE;
    END IF;
    IF p_target IS NULL OR p_target <= current_epoch THEN
        RAISE EXCEPTION 'state_epoch only increases (current %, requested %)',
            current_epoch, p_target USING ERRCODE = 'check_violation';
    END IF;
    IF p_target - current_epoch > 1000 THEN
        RAISE EXCEPTION 'state_epoch can rise by at most 1000 at a time'
            USING ERRCODE = 'check_violation';
    END IF;
    UPDATE mint.state_epoch SET epoch = p_target WHERE instance_id = p_instance_id;
    RETURN p_target;
END $$;

REVOKE ALL ON FUNCTION mint.raise_epoch(text, bigint) FROM PUBLIC;

CREATE FUNCTION mint.restore_attest_epoch(p_instance_id text, p_target bigint) RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
    SELECT mint.raise_epoch(p_instance_id, p_target)
$$;

REVOKE ALL ON FUNCTION mint.restore_attest_epoch(text, bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION mint.restore_attest_epoch(text, bigint) TO mint_control;

-- ---------------------------------------------------------------------------
-- 상태 전이·불변 열·버전·세대 트리거
-- 같은 시점의 BEFORE 트리거는 이름 순으로 실행된다.
-- ---------------------------------------------------------------------------

CREATE TRIGGER intakes_1_status BEFORE INSERT OR UPDATE ON mint.intakes
    FOR EACH ROW EXECUTE FUNCTION mint.check_status(
        'received',
        'received>checking,checking>passed,checking>failed,checking>indeterminate,'
        'passed>decided,failed>decided,indeterminate>decided,'
        'passed>checking,indeterminate>checking');
CREATE TRIGGER intakes_2_frozen BEFORE UPDATE ON mint.intakes
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'org_id', 'server_id', 'source', 'created_at');
CREATE TRIGGER intakes_3_once BEFORE UPDATE ON mint.intakes
    FOR EACH ROW EXECUTE FUNCTION mint.set_once(
        'requested_by', 'decided_by', 'decision', 'decision_reason', 'decided_at');

CREATE TRIGGER intake_checks_1_append_only BEFORE UPDATE OR DELETE ON mint.intake_checks
    FOR EACH ROW EXECUTE FUNCTION mint.reject_mutation();
CREATE TRIGGER intake_checks_2_truncate BEFORE TRUNCATE ON mint.intake_checks
    FOR EACH STATEMENT EXECUTE FUNCTION mint.reject_mutation();

CREATE TRIGGER tool_contracts_1_status BEFORE INSERT OR UPDATE ON mint.tool_contracts
    FOR EACH ROW EXECUTE FUNCTION mint.check_status(
        'candidate', 'candidate>approved,approved>superseded,candidate>superseded');
CREATE TRIGGER tool_contracts_2_frozen BEFORE UPDATE ON mint.tool_contracts
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'server_id', 'credential_scope', 'tool', 'canonical_text', 'sha256',
        'pin_sha256', 'source');
CREATE TRIGGER tool_contracts_3_frozen_after_review BEFORE UPDATE ON mint.tool_contracts
    FOR EACH ROW WHEN (OLD.status <> 'candidate')
    EXECUTE FUNCTION mint.freeze_columns('risk_grade', 'action_type');

CREATE TRIGGER deployment_contracts_1_status BEFORE INSERT OR UPDATE ON mint.deployment_contracts
    FOR EACH ROW EXECUTE FUNCTION mint.check_status(
        'candidate', 'candidate>approved,approved>superseded,candidate>superseded');
CREATE TRIGGER deployment_contracts_2_frozen BEFORE UPDATE ON mint.deployment_contracts
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'server_id', 'canonical_text', 'sha256', 'url', 'url_sha256',
        'transport', 'auth_type', 'issuer', 'oauth_scopes', 'allowed_tools', 'image_digest',
        'source_commit');

CREATE TRIGGER jobs_1_status BEFORE INSERT OR UPDATE ON mint.jobs
    FOR EACH ROW EXECUTE FUNCTION mint.check_status(
        'pending', 'pending>running,running>done,running>failed,running>pending');
CREATE TRIGGER jobs_2_frozen BEFORE UPDATE ON mint.jobs
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'kind', 'target_ref', 'idempotency_key', 'created_at');

CREATE TRIGGER litellm_grants_2_frozen BEFORE UPDATE ON mint.litellm_grants
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'target_type', 'target_id', 'item', 'server_id');

CREATE TRIGGER seal_batches_1_append_only BEFORE UPDATE OR DELETE ON mint.seal_batches
    FOR EACH ROW EXECUTE FUNCTION mint.reject_mutation();
CREATE TRIGGER seal_batches_2_truncate BEFORE TRUNCATE ON mint.seal_batches
    FOR EACH STATEMENT EXECUTE FUNCTION mint.reject_mutation();

CREATE TRIGGER exports_1_status BEFORE INSERT OR UPDATE ON mint.exports
    FOR EACH ROW EXECUTE FUNCTION mint.check_status(
        'pending', 'pending>written,pending>failed,failed>pending');
CREATE TRIGGER exports_2_frozen BEFORE UPDATE ON mint.exports
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'kind', 'state_epoch', 'to_seal_seq', 'payload_sha256', 'path');

CREATE TRIGGER alerts_1_status BEFORE INSERT OR UPDATE ON mint.alerts
    FOR EACH ROW EXECUTE FUNCTION mint.check_status(
        'open', 'open>acknowledged,acknowledged>resolved,open>resolved');
CREATE TRIGGER alerts_2_frozen BEFORE UPDATE ON mint.alerts
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'kind', 'dedup_key', 'first_at');
CREATE TRIGGER alerts_3_once BEFORE UPDATE ON mint.alerts
    FOR EACH ROW EXECUTE FUNCTION mint.set_once('acknowledged_by', 'resolved_at');

CREATE TRIGGER alert_deliveries_1_status BEFORE INSERT OR UPDATE ON mint.alert_deliveries
    FOR EACH ROW EXECUTE FUNCTION mint.check_status(
        'pending', 'pending>sent,pending>failed');
CREATE TRIGGER alert_deliveries_2_frozen BEFORE UPDATE ON mint.alert_deliveries
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'alert_id', 'channel', 'delivery_id');

CREATE TRIGGER console_sessions_2_frozen BEFORE UPDATE ON mint.console_sessions
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'id_hash', 'instance_id', 'subject', 'user_id', 'key_token', 'operator_name', 'role',
        'created_at', 'absolute_expiry');
CREATE TRIGGER console_sessions_3_once BEFORE UPDATE ON mint.console_sessions
    FOR EACH ROW EXECUTE FUNCTION mint.set_once('revoked_at');

CREATE TRIGGER self_confirmations_0_insert BEFORE INSERT ON mint.self_confirmations
    FOR EACH ROW EXECUTE FUNCTION mint.check_self_confirmation_insert();
CREATE TRIGGER self_confirmations_1_status BEFORE INSERT OR UPDATE ON mint.self_confirmations
    FOR EACH ROW EXECUTE FUNCTION mint.check_status(
        'pending',
        'pending>confirmed,pending>rejected,pending>cancelled,pending>expired,'
        'confirmed>consumed,confirmed>cancelled,confirmed>expired');
CREATE TRIGGER self_confirmations_2_frozen BEFORE UPDATE ON mint.self_confirmations
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'org_id', 'principal_id', 'server_id', 'tool', 'args_fp',
        'fp_key_version', 'policy_version_id', 'purpose', 'requested_by_key_token',
        'expires_at', 'created_at');
CREATE TRIGGER self_confirmations_3_once BEFORE UPDATE ON mint.self_confirmations
    FOR EACH ROW EXECUTE FUNCTION mint.set_once(
        'confirmed_by_user_id', 'confirmed_by_key_token', 'confirmed_at',
        'consumed_invocation_id', 'consumed_at');
CREATE TRIGGER self_confirmations_4_display BEFORE UPDATE ON mint.self_confirmations
    FOR EACH ROW EXECUTE FUNCTION mint.clear_self_confirmation_display();
CREATE TRIGGER self_confirmations_5_role BEFORE UPDATE ON mint.self_confirmations
    FOR EACH ROW EXECUTE FUNCTION mint.restrict_self_confirmation();

CREATE TRIGGER intakes_9_version BEFORE UPDATE ON mint.intakes
    FOR EACH ROW EXECUTE FUNCTION mint.bump_version();
CREATE TRIGGER tool_contracts_9_version BEFORE UPDATE ON mint.tool_contracts
    FOR EACH ROW EXECUTE FUNCTION mint.bump_version('version');
CREATE TRIGGER deployment_contracts_9_version BEFORE UPDATE ON mint.deployment_contracts
    FOR EACH ROW EXECUTE FUNCTION mint.bump_version('version');
CREATE TRIGGER litellm_grants_9_version BEFORE UPDATE ON mint.litellm_grants
    FOR EACH ROW EXECUTE FUNCTION mint.bump_version();
CREATE TRIGGER self_confirmations_9_version BEFORE UPDATE ON mint.self_confirmations
    FOR EACH ROW EXECUTE FUNCTION mint.bump_version();

-- 세대는 집행에 영향을 주는 변경만 올린다.
-- servers: 모든 변경. 단 last_observed_at만 바뀌는 갱신은 올리지 않는다.
-- tool_contracts: status·drift 변경. deployment_contracts: status 변경.
-- self_confirmations: 상태 전이(INSERT는 올리지 않는다).
-- 관측 값(tool_contracts.observed_*), 신청·작업·내보내기·경보·세션은 집행에 영향이 없다.
DROP TRIGGER servers_epoch ON mint.servers;
CREATE TRIGGER servers_ins_del_epoch AFTER INSERT OR DELETE ON mint.servers
    FOR EACH ROW EXECUTE FUNCTION mint.bump_epoch();
CREATE TRIGGER servers_update_epoch AFTER UPDATE ON mint.servers
    FOR EACH ROW WHEN (NOT (
        (to_jsonb(OLD) - 'last_observed_at' - 'state_version')
            = (to_jsonb(NEW) - 'last_observed_at' - 'state_version')
        AND OLD.last_observed_at IS DISTINCT FROM NEW.last_observed_at))
    EXECUTE FUNCTION mint.bump_epoch();

CREATE TRIGGER tool_contracts_epoch AFTER UPDATE ON mint.tool_contracts
    FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status OR OLD.drift IS DISTINCT FROM NEW.drift)
    EXECUTE FUNCTION mint.bump_epoch();
CREATE TRIGGER deployment_contracts_epoch AFTER UPDATE ON mint.deployment_contracts
    FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION mint.bump_epoch();
CREATE TRIGGER self_confirmations_epoch AFTER UPDATE ON mint.self_confirmations
    FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION mint.bump_epoch();

-- ---------------------------------------------------------------------------
-- 행 수준 보안: 본인 확인과 세션 표는 직원 화면 역할이 직접 읽지 못한다(직원 함수만 쓴다).
-- 표 소유자와 슈퍼유저는 정책을 받지 않는다.
-- ---------------------------------------------------------------------------

ALTER TABLE mint.self_confirmations ENABLE ROW LEVEL SECURITY;
CREATE POLICY self_confirmations_system ON mint.self_confirmations
    TO mint_plugin, mint_control USING (true) WITH CHECK (true);

ALTER TABLE mint.console_sessions ENABLE ROW LEVEL SECURITY;
CREATE POLICY console_sessions_control ON mint.console_sessions
    TO mint_control USING (true) WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- 권한
-- ---------------------------------------------------------------------------

-- 제어 평면(서비스 함수가 쓰는 모든 것). 작업 등록은 상태 변경과 같은 트랜잭션이다.
GRANT SELECT, INSERT, UPDATE ON
    mint.intakes, mint.deployment_contracts, mint.console_sessions
    TO mint_control;
GRANT SELECT, INSERT ON mint.tool_contracts TO mint_control;
GRANT UPDATE (risk_grade, action_type, status, drift) ON mint.tool_contracts TO mint_control;
GRANT SELECT, INSERT ON mint.intake_checks, mint.jobs TO mint_control;
GRANT SELECT ON
    mint.exports, mint.schedules, mint.litellm_grants, mint.seal_batches, mint.alert_deliveries
    TO mint_control;
GRANT SELECT, UPDATE (status, acknowledged_by, resolved_at) ON mint.alerts TO mint_control;
GRANT SELECT, UPDATE (held, held_at, missing_count, missing_window_start)
    ON mint.litellm_grants TO mint_control;
GRANT SELECT ON mint.self_confirmations TO mint_control;
GRANT UPDATE (status) ON mint.self_confirmations TO mint_control;

-- 작업 장치
GRANT SELECT, INSERT, UPDATE ON mint.jobs TO mint_worker;
GRANT SELECT, INSERT, UPDATE ON mint.schedules TO mint_worker;
GRANT SELECT, INSERT, UPDATE ON mint.litellm_grants TO mint_worker;
GRANT SELECT, INSERT, UPDATE ON mint.exports TO mint_worker;
GRANT SELECT, INSERT, UPDATE ON mint.alert_deliveries TO mint_worker;
GRANT SELECT, INSERT, UPDATE (last_at, count) ON mint.alerts TO mint_worker;
GRANT SELECT ON mint.tool_contracts TO mint_worker;
GRANT UPDATE (observed_sha256, observed_at) ON mint.tool_contracts TO mint_worker;

-- 봉인자
GRANT SELECT, INSERT ON mint.seal_batches TO mint_sealer;

-- 플러그인: 계약 읽기, 내보내기 읽기, 본인 확인 요청 행 추가와 소비.
GRANT SELECT (id, instance_id, server_id, credential_scope, tool, sha256, pin_sha256,
              risk_grade, action_type, status, drift)
    ON mint.tool_contracts TO mint_plugin;
GRANT SELECT ON mint.exports, mint.seal_batches TO mint_plugin;
GRANT SELECT ON mint.self_confirmations TO mint_plugin;
GRANT INSERT (id, instance_id, org_id, principal_id, server_id, tool, args_fp, fp_key_version,
              policy_version_id, purpose, requested_by_key_token, display, expires_at)
    ON mint.self_confirmations TO mint_plugin;
GRANT UPDATE (status, consumed_invocation_id, consumed_at)
    ON mint.self_confirmations TO mint_plugin;

-- ---------------------------------------------------------------------------
-- 직원 화면 함수. 직원 역할은 표 권한 없이 이 함수만 실행한다.
-- 대상 사용자는 mint.staff_user_id 설정에서만 읽고, 본인 행만 바꾸며,
-- 상태 변경과 사건을 같은 트랜잭션에 쓴다.
-- ---------------------------------------------------------------------------

CREATE FUNCTION mint.staff_event(
    p_kind text, p_entity text, p_id text, p_version bigint, p_actor text, p_payload jsonb
) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
    INSERT INTO mint.events (event_id, instance_id, kind, source_key, actor, payload)
    VALUES (gen_random_uuid(), mint.staff_instance(), p_kind,
            p_entity || ':' || p_id || ':' || p_version || ':' || p_kind, p_actor, p_payload)
    ON CONFLICT (instance_id, source_key) DO NOTHING
$$;

-- 본인 확인의 상태를 바꾸고 사건을 남긴다.
CREATE FUNCTION mint.staff_decide_confirmation(
    p_id uuid, p_target text, p_key_token text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
DECLARE
    uid text := mint.staff_user();
    sc mint.self_confirmations;
BEGIN
    SELECT * INTO sc FROM mint.self_confirmations
    WHERE id = p_id AND principal_id = uid AND instance_id = mint.staff_instance()
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no such self confirmation for this user'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_target IN ('confirmed', 'rejected') THEN
        UPDATE mint.self_confirmations
        SET status = p_target, confirmed_by_user_id = uid,
            confirmed_by_key_token = p_key_token, confirmed_at = now()
        WHERE id = p_id RETURNING * INTO sc;
    ELSE
        UPDATE mint.self_confirmations SET status = p_target WHERE id = p_id
        RETURNING * INTO sc;
    END IF;
    PERFORM mint.staff_event(
        CASE p_target WHEN 'confirmed' THEN 'call_approval_approved'
                      WHEN 'rejected' THEN 'call_approval_rejected'
                      ELSE 'call_approval_cancelled' END,
        'call_approval', p_id::text, sc.state_version, uid,
        jsonb_build_object('kind', 'confirmation'));
END $$;

CREATE FUNCTION mint.staff_confirm(p_id uuid, p_key_token text) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
    SELECT mint.staff_decide_confirmation(p_id, 'confirmed', p_key_token)
$$;

CREATE FUNCTION mint.staff_reject(p_id uuid, p_key_token text) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
    SELECT mint.staff_decide_confirmation(p_id, 'rejected', p_key_token)
$$;

CREATE FUNCTION mint.staff_cancel(p_id uuid) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
    SELECT mint.staff_decide_confirmation(p_id, 'cancelled', NULL)
$$;

CREATE FUNCTION mint.staff_grant_consent(
    p_id uuid, p_org_id text, p_server_id text, p_purpose text, p_data_scope text,
    p_text_version text, p_valid_until timestamptz
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
DECLARE
    uid text := mint.staff_user();
BEGIN
    INSERT INTO mint.consents (id, instance_id, org_id, human_user_id, server_id, purpose,
                               data_scope, text_version, source, granted_at, valid_until,
                               registered_by)
    VALUES (p_id, mint.staff_instance(), p_org_id, uid, p_server_id, p_purpose, p_data_scope,
            p_text_version, 'self_service', now(), p_valid_until, uid);
    PERFORM mint.staff_event('consent_granted', 'consent', p_id::text, 1, uid, '{}'::jsonb);
END $$;

CREATE FUNCTION mint.staff_revoke_consent(p_id uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
DECLARE
    uid text := mint.staff_user();
    version bigint;
BEGIN
    UPDATE mint.consents SET revoked_at = now()
    WHERE id = p_id AND human_user_id = uid AND revoked_at IS NULL
      AND instance_id = mint.staff_instance()
    RETURNING state_version INTO version;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no active consent of this user' USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM mint.staff_event('consent_revoked', 'consent', p_id::text, version, uid, '{}'::jsonb);
END $$;

-- 직원 세션을 시작·갱신·종료한다. 시작과 종료는 사건을 남긴다.
CREATE FUNCTION mint.staff_start_session(
    p_id_hash text, p_key_token text, p_idle_expiry timestamptz, p_absolute_expiry timestamptz
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
DECLARE
    uid text := mint.staff_user();
BEGIN
    INSERT INTO mint.console_sessions (id_hash, instance_id, subject, user_id, key_token, role,
                                       idle_expiry, absolute_expiry)
    VALUES (p_id_hash, mint.staff_instance(), 'staff', uid, p_key_token, 'staff',
            p_idle_expiry, p_absolute_expiry);
    PERFORM mint.staff_event('console_session_started', 'console_session', p_id_hash, 1, uid,
                             jsonb_build_object('subject', 'staff'));
END $$;

CREATE FUNCTION mint.staff_touch_session(p_id_hash text, p_idle_expiry timestamptz) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
BEGIN
    UPDATE mint.console_sessions
    SET last_seen_at = now(), idle_expiry = p_idle_expiry
    WHERE id_hash = p_id_hash AND user_id = mint.staff_user() AND subject = 'staff'
      AND instance_id = mint.staff_instance() AND revoked_at IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no active session of this user' USING ERRCODE = 'insufficient_privilege';
    END IF;
END $$;

CREATE FUNCTION mint.staff_end_session(p_id_hash text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
DECLARE
    uid text := mint.staff_user();
BEGIN
    UPDATE mint.console_sessions SET revoked_at = now()
    WHERE id_hash = p_id_hash AND user_id = uid AND subject = 'staff'
      AND instance_id = mint.staff_instance() AND revoked_at IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no active session of this user' USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM mint.staff_event('console_session_ended', 'console_session', p_id_hash, 2, uid,
                             jsonb_build_object('subject', 'staff'));
END $$;

-- 본인 범위 조회
CREATE FUNCTION mint.staff_list_confirmations() RETURNS SETOF mint.self_confirmations
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
    SELECT * FROM mint.self_confirmations
    WHERE principal_id = mint.staff_user() AND instance_id = mint.staff_instance()
    ORDER BY created_at DESC
$$;

CREATE FUNCTION mint.staff_list_consents() RETURNS SETOF mint.consents
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
    SELECT * FROM mint.consents
    WHERE human_user_id = mint.staff_user() AND instance_id = mint.staff_instance()
    ORDER BY granted_at DESC
$$;

CREATE FUNCTION mint.staff_list_events(p_limit integer DEFAULT 100) RETURNS SETOF mint.events
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
    SELECT * FROM mint.events
    WHERE instance_id = mint.staff_instance()
      AND (actor = mint.staff_user() OR subject ->> 'principal_id' = mint.staff_user())
    ORDER BY inserted_at DESC, event_id DESC
    LIMIT least(greatest(p_limit, 1), 1000)
$$;

-- 내부 함수는 실행 권한을 닫고, 공개 함수만 직원 역할에 연다.
REVOKE ALL ON FUNCTION mint.staff_event(text, text, text, bigint, text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION mint.staff_decide_confirmation(uuid, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION mint.staff_confirm(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION mint.staff_reject(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION mint.staff_cancel(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION mint.staff_grant_consent(
    uuid, text, text, text, text, text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION mint.staff_revoke_consent(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION mint.staff_start_session(text, text, timestamptz, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION mint.staff_touch_session(text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION mint.staff_end_session(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION mint.staff_list_confirmations() FROM PUBLIC;
REVOKE ALL ON FUNCTION mint.staff_list_consents() FROM PUBLIC;
REVOKE ALL ON FUNCTION mint.staff_list_events(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    mint.staff_confirm(uuid, text), mint.staff_reject(uuid, text), mint.staff_cancel(uuid),
    mint.staff_grant_consent(uuid, text, text, text, text, text, timestamptz),
    mint.staff_revoke_consent(uuid),
    mint.staff_start_session(text, text, timestamptz, timestamptz),
    mint.staff_touch_session(text, timestamptz), mint.staff_end_session(text),
    mint.staff_list_confirmations(), mint.staff_list_consents(),
    mint.staff_list_events(integer)
    TO mint_staff;
