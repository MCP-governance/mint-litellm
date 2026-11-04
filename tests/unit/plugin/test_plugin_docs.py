from pathlib import Path

import mint_litellm

DOC = Path(mint_litellm.__file__).resolve().parents[2] / "docs" / "보장과-한계.md"


def test_the_limits_document_states_the_tool_list_behaviors_of_the_proxy():
    text = DOC.read_text(encoding="utf-8")
    assert "목록에서 숨긴 도구의 직접 호출" in text
    assert "도구를 찾을 수 없다" in text and "MINT 사유 코드가 없다" in text
    assert "설명과 스키마가 비어 있는 도구" in text
    assert "판정과 사건 없이 목록에 보인다" in text
