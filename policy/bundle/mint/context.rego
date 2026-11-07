package mint.context

import rego.v1

# 플러그인이 패턴 계산에 쓰는 번들 데이터. 판본 대조용 메타와 함께 돌려준다.
bundle := data.mint.meta
secret_markers := object.get(data.mint.data, "secret_markers", [])

# 수용검증이 읽는 데이터(설명 오염 표지, 위험 등급 표).
description_markers := object.get(data.mint.data, "description_markers", [])
risk := object.get(data.mint.data, "risk", {})
