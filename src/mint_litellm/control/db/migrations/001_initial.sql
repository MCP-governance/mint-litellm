-- MINT 초기 스키마. PostgreSQL 15 이상(NULLS NOT DISTINCT)이 필요하다.
-- 모든 객체는 mint 스키마에 만든다. 시각은 UTC(timestamptz)로 저장한다.

CREATE SCHEMA IF NOT EXISTS mint;

-- 사건 키·소스 키에 들어가는 식별자는 구분자(:)와 공백을 포함할 수 없다.
CREATE DOMAIN mint.ident AS text
    CHECK (VALUE ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$');

-- ---------------------------------------------------------------------------
-- 공통 트리거 함수
-- ---------------------------------------------------------------------------

-- 행이 바뀔 때마다 낙관적 잠금 버전을 올린다. 호출자가 지정한 값은 무시된다.
CREATE FUNCTION mint.bump_version() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    col text := COALESCE(TG_ARGV[0], 'state_version');
BEGIN
    NEW := jsonb_populate_record(
        NEW, jsonb_build_object(col, (to_jsonb(OLD) ->> col)::bigint + 1));
    RETURN NEW;
END $$;

-- 집행에 영향을 주는 변경과 같은 트랜잭션에서 배치별 세대를 올린다.
-- 호출 판정을 하는 역할이 호출별 승인을 소비해도 세대가 오르도록 소유자 권한으로 실행한다.
CREATE FUNCTION mint.bump_epoch() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = mint, pg_catalog AS $$
DECLARE
    iid text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        iid := OLD.instance_id;
    ELSE
        iid := NEW.instance_id;
    END IF;
    INSERT INTO mint.state_epoch AS e (instance_id, epoch) VALUES (iid, 1)
    ON CONFLICT (instance_id) DO UPDATE SET epoch = e.epoch + 1;
    RETURN NULL;
END $$;

-- TG_ARGV[0]: 새 행의 허용 상태(쉼표 구분), TG_ARGV[1]: 허용 전이('이전>이후' 쉼표 구분).
-- 목록에 없는 전이는 종결 상태에서 나가는 전이를 포함해 모두 거부한다.
CREATE FUNCTION mint.check_status() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT NEW.status = ANY (string_to_array(TG_ARGV[0], ',')) THEN
            RAISE EXCEPTION '% cannot be created in status %', TG_TABLE_NAME, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT (OLD.status || '>' || NEW.status) = ANY (string_to_array(TG_ARGV[1], ',')) THEN
            RAISE EXCEPTION '% cannot move from % to %', TG_TABLE_NAME, OLD.status, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;

-- TG_ARGV: 생성 뒤 바꿀 수 없는 열 이름.
CREATE FUNCTION mint.freeze_columns() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    col text;
BEGIN
    FOREACH col IN ARRAY TG_ARGV LOOP
        IF to_jsonb(OLD) -> col IS DISTINCT FROM to_jsonb(NEW) -> col THEN
            RAISE EXCEPTION '%.% cannot be changed', TG_TABLE_NAME, col
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
    RETURN NEW;
END $$;

-- TG_ARGV: 값이 한 번 정해지면 바꿀 수 없는 열 이름.
CREATE FUNCTION mint.set_once() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    col text;
BEGIN
    FOREACH col IN ARRAY TG_ARGV LOOP
        IF to_jsonb(OLD) ->> col IS NOT NULL
           AND to_jsonb(OLD) -> col IS DISTINCT FROM to_jsonb(NEW) -> col THEN
            RAISE EXCEPTION '%.% is already set', TG_TABLE_NAME, col
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
    RETURN NEW;
END $$;

-- ---------------------------------------------------------------------------
-- 조직
-- ---------------------------------------------------------------------------

CREATE TABLE mint.orgs (
    instance_id   mint.ident NOT NULL,
    org_id        mint.ident NOT NULL,
    name          text NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed')),
    state_version bigint NOT NULL DEFAULT 1,
    PRIMARY KEY (instance_id, org_id)
);

-- 한 팀·한 사용자는 한 조직에만 묶인다. 제거(unbind)는 행 삭제이며 사건이 이력을 남긴다.
CREATE TABLE mint.org_bindings (
    id            uuid PRIMARY KEY,
    instance_id   mint.ident NOT NULL,
    org_id        mint.ident NOT NULL,
    subject_type  text NOT NULL CHECK (subject_type IN ('team', 'user')),
    subject_id    text NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 256),
    created_by    text NOT NULL,
    state_version bigint NOT NULL DEFAULT 1,
    FOREIGN KEY (instance_id, org_id) REFERENCES mint.orgs (instance_id, org_id),
    UNIQUE (instance_id, subject_type, subject_id)
);

-- ---------------------------------------------------------------------------
-- 서버·이용 관계·허용 목록
-- ---------------------------------------------------------------------------

-- server_id는 LiteLLM 서버 식별자이고 id는 MINT 행 식별자(사건 키에 쓴다).
-- url_sha256·transport·auth_type은 승인된 배포 계약 값이다.
CREATE TABLE mint.servers (
    id                  uuid PRIMARY KEY,
    instance_id         mint.ident NOT NULL,
    server_id           text NOT NULL CHECK (length(server_id) BETWEEN 1 AND 256),
    status              text NOT NULL CHECK (status IN (
        'pending', 'validating', 'awaiting_approval', 'rejected',
        'approved', 'suspended', 'terminating', 'terminated')),
    url_sha256          text NOT NULL CHECK (url_sha256 ~ '^[0-9a-f]{64}$'),
    transport           text NOT NULL CHECK (length(transport) > 0),
    auth_type           text NOT NULL CHECK (length(auth_type) > 0),
    basis_allowlist_ids uuid[] NOT NULL DEFAULT '{}',
    registered_by       text NOT NULL,
    state_version       bigint NOT NULL DEFAULT 1,
    UNIQUE (instance_id, server_id)
);

CREATE TABLE mint.usage_relations (
    id            uuid PRIMARY KEY,
    instance_id   mint.ident NOT NULL,
    org_id        mint.ident NOT NULL,
    team_id       text,
    purpose       text NOT NULL CHECK (length(purpose) BETWEEN 1 AND 256),
    server_id     text NOT NULL,
    status        text NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'terminating', 'terminated')),
    state_version bigint NOT NULL DEFAULT 1,
    FOREIGN KEY (instance_id, org_id) REFERENCES mint.orgs (instance_id, org_id),
    FOREIGN KEY (instance_id, server_id) REFERENCES mint.servers (instance_id, server_id),
    UNIQUE NULLS NOT DISTINCT (instance_id, org_id, team_id, purpose, server_id),
    UNIQUE (id, instance_id, org_id, server_id)
);

-- 종료일이 필수이고 만료·해제된 항목은 조회에서 제외된다.
CREATE TABLE mint.allowlists (
    id            uuid PRIMARY KEY,
    instance_id   mint.ident NOT NULL,
    org_id        mint.ident,
    kind          text NOT NULL CHECK (kind IN (
        'provider', 'auth_server', 'egress_target', 'registry_source')),
    value         text NOT NULL CHECK (length(value) BETWEEN 1 AND 2048),
    note          text,
    valid_from    timestamptz NOT NULL,
    valid_until   timestamptz NOT NULL,
    created_by    text NOT NULL,
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'expired', 'lifted')),
    state_version bigint NOT NULL DEFAULT 1,
    FOREIGN KEY (instance_id, org_id) REFERENCES mint.orgs (instance_id, org_id),
    CHECK (valid_from < valid_until)
);

CREATE UNIQUE INDEX allowlists_active_value
    ON mint.allowlists (instance_id, org_id, kind, value) NULLS NOT DISTINCT
    WHERE status = 'active';

-- ---------------------------------------------------------------------------
-- 정책 판본·설정·운영자
-- ---------------------------------------------------------------------------

CREATE TABLE mint.policy_versions (
    instance_id    mint.ident NOT NULL,
    id             mint.ident NOT NULL,
    catalog_sha256 text NOT NULL CHECK (catalog_sha256 ~ '^[0-9a-f]{64}$'),
    bundle_sha256  text NOT NULL CHECK (bundle_sha256 ~ '^[0-9a-f]{64}$'),
    status         text NOT NULL DEFAULT 'validated'
                   CHECK (status IN ('validated', 'staged', 'active', 'superseded')),
    activated_at   timestamptz,
    activated_by   text,
    state_version  bigint NOT NULL DEFAULT 1,
    PRIMARY KEY (instance_id, id),
    CHECK (status <> 'active' OR (activated_at IS NOT NULL AND activated_by IS NOT NULL))
);

-- 배치마다 활성 판본은 하나뿐이다.
CREATE UNIQUE INDEX policy_versions_one_active
    ON mint.policy_versions (instance_id) WHERE status = 'active';

CREATE TABLE mint.settings (
    instance_id           mint.ident PRIMARY KEY,
    mode                  text NOT NULL DEFAULT 'enforce' CHECK (mode IN ('enforce', 'observe')),
    observable_reasons    text[] NOT NULL DEFAULT ARRAY[
        'SERVER_UNREGISTERED', 'SERVER_NOT_APPROVED', 'APPROVAL_ABSENT',
        'TOOL_NOT_IN_APPROVAL', 'POLICY_DETECT', 'CONTRACT_DRIFT', 'OUTPUT_SCAN'],
    observable_policy_ids text[] NOT NULL DEFAULT '{}',
    default_org_id        mint.ident,
    settings_version      bigint NOT NULL DEFAULT 1,
    FOREIGN KEY (instance_id, default_org_id) REFERENCES mint.orgs (instance_id, org_id),
    CHECK (observable_reasons <@ ARRAY[
        'SERVER_UNREGISTERED', 'SERVER_NOT_APPROVED', 'APPROVAL_ABSENT',
        'TOOL_NOT_IN_APPROVAL', 'POLICY_DETECT', 'CONTRACT_DRIFT', 'OUTPUT_SCAN'])
);

-- admin·approver는 LiteLLM 사람 사용자 식별자에 결속되어야 하고, 직무 분리는 user_id로 판정한다.
CREATE TABLE mint.operators (
    instance_id   mint.ident NOT NULL,
    name          mint.ident NOT NULL,
    role          text NOT NULL CHECK (role IN ('admin', 'approver', 'viewer')),
    token_hash    text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    user_id       text CHECK (user_id IS NULL OR length(user_id) > 0),
    expires_at    timestamptz,
    state_version bigint NOT NULL DEFAULT 1,
    PRIMARY KEY (instance_id, name),
    CHECK (role = 'viewer' OR user_id IS NOT NULL)
);

-- ---------------------------------------------------------------------------
-- 승인·거부·호출별 승인·동의
-- ---------------------------------------------------------------------------

-- 승인은 생성 뒤 기간·범위·도구를 바꿀 수 없다. 연장은 새 승인으로만 한다.
CREATE TABLE mint.approvals (
    id            uuid PRIMARY KEY,
    instance_id   mint.ident NOT NULL,
    org_id        mint.ident NOT NULL,
    relation_id   uuid NOT NULL,
    class         text NOT NULL CHECK (class IN ('standard', 'exception')),
    server_id     text NOT NULL,
    tools         text[] NOT NULL CHECK (cardinality(tools) > 0),
    scope_type    text NOT NULL CHECK (scope_type IN ('key', 'user', 'team', 'org')),
    scope_id      text NOT NULL CHECK (length(scope_id) > 0),
    valid_from    timestamptz NOT NULL,
    valid_until   timestamptz NOT NULL,
    conditions    jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(conditions) = 'object'),
    basis         jsonb NOT NULL CHECK (jsonb_typeof(basis) = 'object'),
    granted_by    text,
    requested_by  text NOT NULL,
    status        text NOT NULL CHECK (status IN ('pending', 'active', 'expired', 'revoked')),
    revoked_at    timestamptz,
    revoke_reason text,
    state_version bigint NOT NULL DEFAULT 1,
    FOREIGN KEY (instance_id, org_id) REFERENCES mint.orgs (instance_id, org_id),
    FOREIGN KEY (relation_id, instance_id, org_id, server_id)
        REFERENCES mint.usage_relations (id, instance_id, org_id, server_id),
    CHECK (valid_from < valid_until),
    CHECK (status = 'pending' OR granted_by IS NOT NULL),
    CHECK ((status = 'revoked') = (revoked_at IS NOT NULL))
);

CREATE INDEX approvals_lookup
    ON mint.approvals (instance_id, server_id, scope_type, scope_id) WHERE status <> 'pending';

CREATE TABLE mint.denials (
    id            uuid PRIMARY KEY,
    instance_id   mint.ident NOT NULL,
    org_id        mint.ident,
    scope_type    text NOT NULL CHECK (scope_type IN ('key', 'user', 'team', 'org', 'server')),
    scope_id      text NOT NULL CHECK (length(scope_id) > 0),
    server_id     text,
    tools         text[] CHECK (tools IS NULL OR cardinality(tools) > 0),
    reason        text NOT NULL CHECK (length(reason) > 0),
    created_by    text NOT NULL,
    state_version bigint NOT NULL DEFAULT 1,
    lifted_at     timestamptz,
    lifted_by     text,
    FOREIGN KEY (instance_id, org_id) REFERENCES mint.orgs (instance_id, org_id),
    FOREIGN KEY (instance_id, server_id) REFERENCES mint.servers (instance_id, server_id),
    CHECK (scope_type <> 'server' OR (server_id IS NOT NULL AND scope_id = server_id)),
    CHECK ((lifted_at IS NULL) = (lifted_by IS NULL))
);

CREATE INDEX denials_active
    ON mint.denials (instance_id, scope_type, scope_id) WHERE lifted_at IS NULL;

CREATE TABLE mint.call_approvals (
    id                     uuid PRIMARY KEY,
    instance_id            mint.ident NOT NULL,
    org_id                 mint.ident NOT NULL,
    principal_type         text NOT NULL
                           CHECK (principal_type IN ('user', 'team_key', 'service_account')),
    principal_id           text NOT NULL CHECK (length(principal_id) > 0),
    server_id              text NOT NULL,
    tool                   text NOT NULL CHECK (length(tool) > 0),
    args_fp                text NOT NULL CHECK (args_fp ~ '^[0-9a-f]{64}$'),
    fp_key_version         integer NOT NULL CHECK (fp_key_version >= 1),
    policy_version_id      mint.ident NOT NULL,
    contract_version       text,
    status                 text NOT NULL CHECK (status IN (
        'pending', 'approved', 'rejected', 'cancelled', 'expired', 'consumed')),
    requested_by           text NOT NULL,
    approved_by            text,
    expires_at             timestamptz NOT NULL,
    consumed_invocation_id uuid,
    state_version          bigint NOT NULL DEFAULT 1,
    FOREIGN KEY (instance_id, org_id) REFERENCES mint.orgs (instance_id, org_id),
    FOREIGN KEY (instance_id, server_id) REFERENCES mint.servers (instance_id, server_id),
    FOREIGN KEY (instance_id, policy_version_id)
        REFERENCES mint.policy_versions (instance_id, id),
    CHECK (status NOT IN ('approved', 'consumed') OR approved_by IS NOT NULL),
    CHECK ((status = 'consumed') = (consumed_invocation_id IS NOT NULL))
);

-- 한 호출은 호출별 승인을 하나만 소비한다.
CREATE UNIQUE INDEX call_approvals_consumed_once
    ON mint.call_approvals (instance_id, consumed_invocation_id)
    WHERE consumed_invocation_id IS NOT NULL;

CREATE INDEX call_approvals_open
    ON mint.call_approvals (instance_id, principal_type, principal_id, server_id, tool)
    WHERE status = 'approved';

-- 운영자 증명 동의는 증거 참조와 증거 해시가 있어야 등록된다.
CREATE TABLE mint.consents (
    id              uuid PRIMARY KEY,
    instance_id     mint.ident NOT NULL,
    org_id          mint.ident NOT NULL,
    human_user_id   text NOT NULL CHECK (length(human_user_id) > 0),
    server_id       text NOT NULL,
    purpose         text NOT NULL CHECK (length(purpose) BETWEEN 1 AND 128),
    data_scope      text NOT NULL CHECK (length(data_scope) > 0),
    text_version    text NOT NULL CHECK (length(text_version) > 0),
    source          text NOT NULL CHECK (source IN ('self_service', 'operator_attested')),
    evidence_ref    text,
    evidence_sha256 text CHECK (evidence_sha256 IS NULL OR evidence_sha256 ~ '^[0-9a-f]{64}$'),
    granted_at      timestamptz NOT NULL,
    valid_until     timestamptz NOT NULL,
    revoked_at      timestamptz,
    registered_by   text NOT NULL,
    state_version   bigint NOT NULL DEFAULT 1,
    FOREIGN KEY (instance_id, org_id) REFERENCES mint.orgs (instance_id, org_id),
    FOREIGN KEY (instance_id, server_id) REFERENCES mint.servers (instance_id, server_id),
    CHECK (granted_at < valid_until),
    CHECK (source <> 'operator_attested'
           OR (evidence_ref IS NOT NULL AND length(evidence_ref) > 0
               AND evidence_sha256 IS NOT NULL))
);

CREATE INDEX consents_lookup
    ON mint.consents (instance_id, human_user_id, server_id, purpose);

-- ---------------------------------------------------------------------------
-- 세대·원장
-- ---------------------------------------------------------------------------

-- 배치마다 한 행. 증가만 가능하며 행은 삭제할 수 없다.
CREATE TABLE mint.state_epoch (
    instance_id mint.ident PRIMARY KEY,
    epoch       bigint NOT NULL DEFAULT 0 CHECK (epoch >= 0)
);

CREATE FUNCTION mint.guard_state_epoch() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.epoch > OLD.epoch AND NEW.instance_id = OLD.instance_id THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'state_epoch only increases' USING ERRCODE = 'check_violation';
END $$;

CREATE TRIGGER state_epoch_guard
    BEFORE UPDATE OR DELETE ON mint.state_epoch
    FOR EACH ROW EXECUTE FUNCTION mint.guard_state_epoch();

-- 같은 배치에서 source_key가 같은 사건은 한 행으로 수렴한다.
-- 재적재는 ON CONFLICT (instance_id, source_key) DO NOTHING.
CREATE TABLE mint.events (
    event_id      uuid PRIMARY KEY,
    instance_id   mint.ident NOT NULL,
    inserted_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    kind          text NOT NULL CHECK (kind ~ '^[a-z][a-z_]*$'),
    source_key    text NOT NULL CHECK (length(source_key) BETWEEN 1 AND 512),
    invocation_id uuid,
    attempt_id    integer CHECK (attempt_id >= 1),
    actor         text,
    subject       jsonb,
    payload       jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    seal_seq      bigint,
    prev_hash     bytea CHECK (prev_hash IS NULL OR length(prev_hash) = 32),
    row_hash      bytea CHECK (row_hash IS NULL OR length(row_hash) = 32),
    sealed_at     timestamptz,
    CHECK (source_key NOT LIKE 'call:%' OR (invocation_id IS NOT NULL AND attempt_id IS NOT NULL)),
    CHECK ((seal_seq IS NULL) = (row_hash IS NULL) AND (seal_seq IS NULL) = (sealed_at IS NULL)),
    UNIQUE (instance_id, source_key),
    UNIQUE (instance_id, seal_seq)
);

CREATE INDEX events_by_time ON mint.events (instance_id, inserted_at);
CREATE INDEX events_by_invocation ON mint.events (invocation_id) WHERE invocation_id IS NOT NULL;
CREATE INDEX events_unsealed ON mint.events (inserted_at, event_id) WHERE seal_seq IS NULL;

-- 원장은 추가만 가능하다. 봉인 열은 삽입 때 지워지고 봉인자의 수정으로만 한 번 채워진다.
CREATE FUNCTION mint.guard_events() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    seal_cols text[] := ARRAY['seal_seq', 'prev_hash', 'row_hash', 'sealed_at'];
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.seal_seq := NULL;
        NEW.prev_hash := NULL;
        NEW.row_hash := NULL;
        NEW.sealed_at := NULL;
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF OLD.seal_seq IS NULL AND (to_jsonb(NEW) - seal_cols) = (to_jsonb(OLD) - seal_cols) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'events are append-only; only unsealed seal columns can be filled'
            USING ERRCODE = 'check_violation';
    END IF;
    RAISE EXCEPTION 'events cannot be deleted' USING ERRCODE = 'check_violation';
END $$;

CREATE TRIGGER events_guard
    BEFORE INSERT OR UPDATE OR DELETE ON mint.events
    FOR EACH ROW EXECUTE FUNCTION mint.guard_events();

CREATE FUNCTION mint.guard_events_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'events cannot be truncated' USING ERRCODE = 'check_violation';
END $$;

CREATE TRIGGER events_guard_truncate
    BEFORE TRUNCATE ON mint.events
    FOR EACH STATEMENT EXECUTE FUNCTION mint.guard_events_truncate();

-- ---------------------------------------------------------------------------
-- 상태 전이·불변 열·세대·버전 트리거
-- 같은 시점의 BEFORE 트리거는 이름 순으로 실행된다.
-- ---------------------------------------------------------------------------

CREATE TRIGGER orgs_1_status BEFORE INSERT OR UPDATE ON mint.orgs
    FOR EACH ROW EXECUTE FUNCTION mint.check_status('active', 'active>closed');
CREATE TRIGGER orgs_2_frozen BEFORE UPDATE ON mint.orgs
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns('instance_id', 'org_id');

CREATE TRIGGER org_bindings_2_frozen BEFORE UPDATE ON mint.org_bindings
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'org_id', 'subject_type', 'subject_id', 'created_by');

CREATE TRIGGER servers_1_status BEFORE INSERT OR UPDATE ON mint.servers
    FOR EACH ROW EXECUTE FUNCTION mint.check_status(
        'pending,awaiting_approval',
        'pending>validating,validating>awaiting_approval,validating>rejected,'
        'awaiting_approval>approved,approved>suspended,suspended>approved,'
        'approved>terminating,suspended>terminating,terminating>terminated,'
        'rejected>validating');
CREATE TRIGGER servers_2_frozen BEFORE UPDATE ON mint.servers
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns('instance_id', 'server_id');

-- 배열 열은 외래 키를 걸 수 없으므로, 승인 근거로 저장한 허용 목록 항목이 같은 배치에
-- 실제로 있는지 저장 때 확인한다.
CREATE FUNCTION mint.check_basis_allowlists() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    wanted uuid[];
BEGIN
    SELECT array_agg(DISTINCT ref) INTO wanted FROM unnest(NEW.basis_allowlist_ids) AS ref;
    IF wanted IS NOT NULL AND cardinality(wanted) <> (
        SELECT count(*) FROM mint.allowlists a
        WHERE a.instance_id = NEW.instance_id AND a.id = ANY (wanted)
    ) THEN
        RAISE EXCEPTION 'basis_allowlist_ids must reference allowlists of the same instance'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER servers_3_basis BEFORE INSERT OR UPDATE OF basis_allowlist_ids ON mint.servers
    FOR EACH ROW EXECUTE FUNCTION mint.check_basis_allowlists();

CREATE TRIGGER usage_relations_1_status BEFORE INSERT OR UPDATE ON mint.usage_relations
    FOR EACH ROW EXECUTE FUNCTION mint.check_status(
        'active', 'active>terminating,terminating>terminated');
CREATE TRIGGER usage_relations_2_frozen BEFORE UPDATE ON mint.usage_relations
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'org_id', 'team_id', 'purpose', 'server_id');

CREATE TRIGGER allowlists_1_status BEFORE INSERT OR UPDATE ON mint.allowlists
    FOR EACH ROW EXECUTE FUNCTION mint.check_status('active', 'active>expired,active>lifted');
CREATE TRIGGER allowlists_2_frozen BEFORE UPDATE ON mint.allowlists
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'org_id', 'kind', 'value', 'valid_from', 'valid_until', 'created_by');

CREATE TRIGGER policy_versions_1_status BEFORE INSERT OR UPDATE ON mint.policy_versions
    FOR EACH ROW EXECUTE FUNCTION mint.check_status(
        'validated', 'validated>staged,staged>active,active>superseded');
CREATE TRIGGER policy_versions_2_frozen BEFORE UPDATE ON mint.policy_versions
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'id', 'catalog_sha256', 'bundle_sha256');

CREATE TRIGGER settings_2_frozen BEFORE UPDATE ON mint.settings
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns('instance_id');

CREATE TRIGGER approvals_1_status BEFORE INSERT OR UPDATE ON mint.approvals
    FOR EACH ROW EXECUTE FUNCTION mint.check_status(
        'pending,active', 'pending>active,active>expired,active>revoked');
CREATE TRIGGER approvals_2_frozen BEFORE UPDATE ON mint.approvals
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'org_id', 'relation_id', 'class', 'server_id', 'tools', 'scope_type',
        'scope_id', 'valid_from', 'valid_until', 'conditions', 'basis', 'requested_by');
CREATE TRIGGER approvals_3_once BEFORE UPDATE ON mint.approvals
    FOR EACH ROW EXECUTE FUNCTION mint.set_once('granted_by', 'revoked_at', 'revoke_reason');

CREATE TRIGGER denials_2_frozen BEFORE UPDATE ON mint.denials
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'org_id', 'scope_type', 'scope_id', 'server_id', 'tools', 'reason',
        'created_by');
CREATE TRIGGER denials_3_once BEFORE UPDATE ON mint.denials
    FOR EACH ROW EXECUTE FUNCTION mint.set_once('lifted_at', 'lifted_by');

-- 판정 프로세스 역할만 가진 접속은 승인된 호출별 승인을 소비하는 전이만 할 수 있다.
-- 제어 평면 역할의 구성원과 슈퍼유저는 제한을 받지 않는다.
CREATE FUNCTION mint.restrict_plugin_call_approval() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF pg_has_role(current_user, 'mint_plugin', 'MEMBER')
       AND NOT pg_has_role(current_user, 'mint_control', 'MEMBER')
       AND NOT (OLD.status = 'approved' AND NEW.status = 'consumed'
                AND NEW.consumed_invocation_id IS NOT NULL) THEN
        RAISE EXCEPTION 'the plugin role can only consume an approved call approval'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER call_approvals_0_role BEFORE UPDATE ON mint.call_approvals
    FOR EACH ROW EXECUTE FUNCTION mint.restrict_plugin_call_approval();
CREATE TRIGGER call_approvals_1_status BEFORE INSERT OR UPDATE ON mint.call_approvals
    FOR EACH ROW EXECUTE FUNCTION mint.check_status(
        'pending',
        'pending>approved,approved>consumed,pending>rejected,pending>cancelled,'
        'approved>cancelled,pending>expired,approved>expired');
CREATE TRIGGER call_approvals_2_frozen BEFORE UPDATE ON mint.call_approvals
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'org_id', 'principal_type', 'principal_id', 'server_id', 'tool',
        'args_fp', 'fp_key_version', 'policy_version_id', 'contract_version', 'requested_by',
        'expires_at');
CREATE TRIGGER call_approvals_3_once BEFORE UPDATE ON mint.call_approvals
    FOR EACH ROW EXECUTE FUNCTION mint.set_once('approved_by', 'consumed_invocation_id');

CREATE TRIGGER consents_2_frozen BEFORE UPDATE ON mint.consents
    FOR EACH ROW EXECUTE FUNCTION mint.freeze_columns(
        'instance_id', 'org_id', 'human_user_id', 'server_id', 'purpose', 'data_scope',
        'text_version', 'source', 'evidence_ref', 'evidence_sha256', 'granted_at',
        'valid_until', 'registered_by');
CREATE TRIGGER consents_3_once BEFORE UPDATE ON mint.consents
    FOR EACH ROW EXECUTE FUNCTION mint.set_once('revoked_at');

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'orgs', 'org_bindings', 'servers', 'usage_relations', 'allowlists', 'policy_versions',
        'approvals', 'denials', 'call_approvals', 'consents', 'operators'] LOOP
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE ON mint.%I '
            'FOR EACH ROW EXECUTE FUNCTION mint.bump_version()', t || '_9_version', t);
    END LOOP;
    -- 집행에 영향을 주는 표(운영자 표 제외)
    FOREACH t IN ARRAY ARRAY[
        'orgs', 'org_bindings', 'servers', 'usage_relations', 'allowlists', 'policy_versions',
        'settings', 'approvals', 'denials', 'call_approvals', 'consents'] LOOP
        EXECUTE format(
            'CREATE TRIGGER %I AFTER INSERT OR UPDATE OR DELETE ON mint.%I '
            'FOR EACH ROW EXECUTE FUNCTION mint.bump_epoch()', t || '_epoch', t);
    END LOOP;
END $$;

CREATE TRIGGER settings_9_version BEFORE UPDATE ON mint.settings
    FOR EACH ROW EXECUTE FUNCTION mint.bump_version('settings_version');

-- ---------------------------------------------------------------------------
-- 역할 분리
--   mint_ledger_writer: 원장 추가·조회
--   mint_sealer:        봉인 열 채우기
--   mint_plugin:        판정 프로세스. 상태 조회와 호출별 승인 소비만 가능
--   mint_control:       제어 평면. 상태 변경 가능, 원장 추가
-- 로그인 역할은 운영자가 만들어 위 역할 중 하나를 부여한다.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['mint_ledger_writer', 'mint_sealer', 'mint_plugin', 'mint_control'] LOOP
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', r);
        END IF;
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA mint
    TO mint_ledger_writer, mint_sealer, mint_plugin, mint_control;

GRANT SELECT, INSERT ON mint.events TO mint_ledger_writer;
GRANT SELECT ON mint.events TO mint_sealer;
GRANT UPDATE (seal_seq, prev_hash, row_hash, sealed_at) ON mint.events TO mint_sealer;

GRANT mint_ledger_writer TO mint_plugin, mint_control;

GRANT SELECT ON
    mint.orgs, mint.org_bindings, mint.servers, mint.usage_relations, mint.allowlists,
    mint.approvals, mint.denials, mint.call_approvals, mint.consents, mint.state_epoch,
    mint.settings, mint.policy_versions
    TO mint_plugin;
GRANT UPDATE (status, consumed_invocation_id) ON mint.call_approvals TO mint_plugin;

GRANT SELECT, INSERT, UPDATE ON
    mint.orgs, mint.org_bindings, mint.servers, mint.usage_relations, mint.allowlists,
    mint.approvals, mint.denials, mint.call_approvals, mint.consents, mint.settings,
    mint.policy_versions, mint.operators
    TO mint_control;
GRANT SELECT ON mint.state_epoch TO mint_control;
GRANT DELETE ON mint.org_bindings TO mint_control;
