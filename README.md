# MINT for LiteLLM

LiteLLM 프록시에 플러그인으로 붙어 MCP 서버와 도구 호출에 거버넌스를 더하는 Python 패키지입니다.

- 도입 수용검증 → 승인 → 호출 판정 → 변경 재승인 → 종료 판정 → 원장
- LiteLLM이 이미 제공하는 권한·전송·자격 저장·속도 제한은 다시 만들지 않고 그대로 씁니다.
- 판정 불능이면 차단합니다(fail-closed).

## 구성

| 구성요소 | 역할 |
| --- | --- |
| 플러그인 | LiteLLM 가드레일 훅(`pre_mcp_call`·`post_mcp_call`)에서 판정·기록·결과 검사 |
| 제어 평면·콘솔 | 수용검증, 승인, 계약 변경 재승인, 종료 판정, 원장, 경보 |
| 정책 엔진 | OPA(Rego) 정책 패키지 |
| 배포 | compose 묶음(LiteLLM·PostgreSQL·OPA·콘솔)과 `mint-litellm` CLI |

## 지원 판본

LiteLLM v1.106.6

## 상태

개발 중입니다. 배포 가능한 판본은 아직 없습니다.

## 라이선스

MIT
