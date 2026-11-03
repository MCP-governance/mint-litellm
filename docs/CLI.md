# CLI

`mint-litellm`은 운영자용 명령줄 도구이다. `init`을 제외한 모든 명령은 운영자 토큰으로 인증하며, 변경의 행위자는 토큰에 결속된 운영자 이름이다. 모든 변경은 원장에 사건으로 남는다. CLI는 LiteLLM에 쓰지 않는다. LiteLLM 관리 API는 읽기 확인에만 쓴다.

## 역할

| 역할 | 권한 |
| --- | --- |
| `admin` | 모든 명령 |
| `approver` | 승인·거부·철회·호출별 승인과 조회 |
| `viewer` | 조회 |

`admin`과 `approver`는 LiteLLM 사람 사용자 식별자(`--user-id`)에 결속되어야 하고, 등록 시 사용자가 존재하는지 확인한다. 직무 분리는 이 식별자로 판정한다. `approve`는 신청·요청자와 같은 사용자의 운영자가 승인할 수 없고(`--second-approver`로 다른 운영자의 토큰을 함께 제시하면 두 사람 모두 기록하고 허용), 호출별 승인은 호출 주체와 같은 사용자의 운영자가 승인할 수 없다. 같은 사람이 토큰을 여러 개 가져도 사용자 식별자가 같으면 한 사람으로 본다.

## 명령

| 명령 | 역할 | 설명 | LiteLLM 읽기 | 사건 |
| --- | --- | --- | --- | --- |
| `mint-litellm init` | 토큰 불필요 | LiteLLM 설정 조각(권장 설정 포함)을 만들고 기본 조직을 준비한다. `--no-spend-logs`를 주면 `disable_spend_logs: true`를 포함한다. | - | - |
| `mint-litellm check` | 모든 역할 | 필수 설정 위반이 있으면 종료 코드 0이 아닌 값으로 끝나고, 권장 설정은 경고하며, 관측 항목 3가지를 출력한다. | `/v1/mcp/server`, `/key/list`, `/team/list`, `/guardrails/list` | - |
| `mint-litellm status` | 모든 역할 | 모드, 정책 판본, 경보, 관측 항목을 표시한다. | `/v1/mcp/server`, `/key/list`, `/team/list` | - |
| `mint-litellm operator add` | `admin` | 운영자를 등록하고 토큰을 발급한다. `admin`·`approver`는 LiteLLM 사용자 식별자 (`--user-id`) 결속이 필수이다. | `/user/info` | `operator_added` |
| `mint-litellm server register` | `admin` | LiteLLM에 등록된 서버를 승인 대기 상태로 등록한다. | `/v1/mcp/server/{server_id}` | `server_registered` |
| `mint-litellm server suspend` | `admin` | 서버를 중지한다. 중지된 서버의 호출은 차단된다. | - | `server_suspended` |
| `mint-litellm server resume` | `admin` | 중지한 서버를 다시 승인 상태로 되돌린다. | - | `server_resumed` |
| `mint-litellm relation add` | `admin` | 조직·팀·목적·서버를 묶는 이용 관계를 만든다. | - | `relation_created` |
| `mint-litellm approve` | `admin`, `approver` | 이용 관계에 속한 승인을 등록한다. 근거와 유효 기간이 필수이며, 대상 권한이 LiteLLM에 서버 식별자 형식으로 있는지 읽어 확인한다. | `/key/info`, `/team/info` | `approval_granted`, `server_approved` |
| `mint-litellm deny` | `admin`, `approver` | 주체 또는 서버에 대한 거부를 만든다. 거부는 모든 승인보다 우선한다. | - | `denial_created` |
| `mint-litellm denial lift` | `admin`, `approver` | 거부를 해제한다. | - | `denial_lifted` |
| `mint-litellm revoke` | `admin`, `approver` | 승인을 철회한다. `--deny`를 주면 같은 범위의 거부를 함께 만든다. | - | `approval_revoked`, `denial_created` |
| `mint-litellm call-approval request` | `admin`, `approver` | 특정 주체·서버·도구·인자에 대한 호출별 승인을 요청한다. | - | `call_approval_requested` |
| `mint-litellm call-approval approve` | `admin`, `approver` | 호출별 승인을 승인한다. 호출 주체와 같은 사용자의 운영자는 승인할 수 없다. | - | `call_approval_approved` |
| `mint-litellm call-approval reject` | `admin`, `approver` | 호출별 승인 요청을 거절한다. | - | `call_approval_rejected` |
| `mint-litellm call-approval cancel` | `admin`, `approver` | 요청 중이거나 승인된 호출별 승인을 취소한다. | - | `call_approval_cancelled` |
| `mint-litellm consent add` | `admin` | 사용자 동의를 등록한다. 운영자 증명은 증거 참조와 증거 해시가 필수이다. | - | `consent_granted` |
| `mint-litellm consent revoke` | `admin` | 등록된 동의를 철회한다. | - | `consent_revoked` |
| `mint-litellm allowlist add` | `admin` | 허용 목록 항목을 등록한다. 종료일이 필수이다. | - | `allowlist_added` |
| `mint-litellm allowlist lift` | `admin` | 허용 목록 항목을 해제한다. | - | `allowlist_lifted` |
| `mint-litellm org add` | `admin` | 조직을 만든다. | - | `org_created` |
| `mint-litellm org close` | `admin` | 조직을 닫는다. 닫힌 조직에 속한 주체의 호출은 차단된다. | - | `org_closed` |
| `mint-litellm org bind` | `admin` | 팀 또는 사용자를 조직에 묶는다. 한 팀·한 사용자는 한 조직에만 묶인다. | - | `org_binding_created` |
| `mint-litellm org unbind` | `admin` | 팀 또는 사용자의 조직 바인딩을 제거한다. | - | `org_binding_removed` |
| `mint-litellm settings default-org` | `admin` | 바인딩이 없는 주체에 적용할 기본 조직을 지정하거나 해제한다. | - | `settings_changed` |
| `mint-litellm settings mode` | `admin` | 집행 모드를 enforce 또는 observe로 정한다. 환경 변수의 상한이 enforce이면 observe로 바꿀 수 없다. | - | `settings_changed` |
| `mint-litellm settings observable` | `admin` | 관측 모드로 전환할 수 있는 사유 계열과 정책을 정한다. | - | `settings_changed` |
| `mint-litellm policy activate` | `admin` | 정책 번들을 검증하고 정책 엔진에 적재한 뒤 활성 판본으로 전환한다. | - | `policy_activated` |
| `mint-litellm ledger list` | 모든 역할 | 원장 사건을 종류·기간·호출 식별자로 조회한다. | - | - |

## 서버와 승인의 활성화 순서

1. LiteLLM에 서버를 구성한다. 설정 서버는 `server_id`를 명시한다.
2. `server register <server_id>`로 서버를 등록한다. LiteLLM 관리 API로 존재를 확인하고 상태는 승인 대기가 된다.
3. `relation add`로 조직·팀·목적과 서버를 묶는 이용 관계를 만든다.
4. `approve --relation <관계> --basis <근거>`로 승인한다. 대상 키·팀의 `object_permission`이 서버 식별자 형식으로 부여되어 있는지 읽어 확인하며, 별칭이나 이름 형식의 항목이 있으면 실패하고 교정 방법을 안내한다. 첫 승인이 활성화되는 같은 트랜잭션에서 서버가 승인 상태로 바뀐다.

승인을 모두 철회해도 서버 상태는 바뀌지 않는다. 서버 전체를 막으려면 `server suspend`를 쓴다. 특정 주체를 더 넓은 승인까지 포함해 막으려면 `revoke --deny`를 쓴다.

## 종료 코드

`check`는 필수 설정이 하나라도 어긋나면 0이 아닌 값으로 끝난다. 권장 설정 위반은 경고로 출력하고 종료 코드에 영향을 주지 않는다.
