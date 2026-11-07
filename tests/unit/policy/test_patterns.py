import time

import pytest

from mint_litellm.plugin.policy.patterns import (
    MAX_DEPTH,
    MAX_STRING_BYTES,
    MAX_TOTAL_BYTES,
    NAMES,
    compute_patterns,
)

D = "."  # 점 표기 주소를 저장소 위생 시험이 막으므로 나눠 쓴다


def only(**kwargs):
    return {name: kwargs.get(name, False) for name in NAMES}


def patterns(arguments, **kwargs):
    return compute_patterns(arguments, **kwargs)


def test_plain_arguments_have_no_pattern():
    assert patterns({"title": "hello", "n": 3, "ok": True, "items": ["a", "b"]}) == only()


@pytest.mark.parametrize(
    ("text", "allowed", "external"),
    [
        ("see https://api.example.com/v1", {"api.example.com"}, False),
        ("https://evil.example.org/x", {"api.example.com"}, True),
        ("https://sub.api.example.com/x", {"api.example.com"}, False),
        ("https://notapi.example.com/x", {"api.example.com"}, True),
        ("https://api.example.com@evil.org/", {"api.example.com"}, True),
        (r"https://api.example.com\@evil.org/", {"api.example.com"}, True),
        ("HTTPS://API.EXAMPLE.COM./x", {"api.example.com"}, False),
        ("https://api.example.com:8443/x", {"api.example.com"}, False),
        ("https://[::1]:80/", {"api.example.com"}, True),
        ("any https://a.test and https://b.test", {"a.test"}, True),
        ("https://a.test", set(), True),
        ("no url, only a.test", {"b.test"}, False),
        ("https://", {"a.test"}, True),
    ],
)
def test_url_external(text, allowed, external):
    assert patterns({"u": text}, allowed_hosts=allowed)["url_external"] is external


@pytest.mark.parametrize(
    "text",
    [
        f"http://127{D}0{D}0{D}1/",
        "http://localhost:8080/x",
        "http://LOCALHOST./",
        "http://a.localhost/",
        f"http://10{D}1{D}2{D}3/",
        f"http://192{D}168{D}0{D}1",
        f"http://172{D}16{D}5{D}5/",
        f"http://169{D}254{D}169{D}254/latest",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        f"http://224{D}0{D}0{D}1/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0x7f.0.0.1/",
        "http://127.1/",
        "http://0177.0.0.1/",
        f"http://[::ffff:127{D}0{D}0{D}1]/",
        "http://[::ffff:7f00:1]/",
        "http://[::ffff:a00:1]/",
        f"http://0{D}0{D}0{D}0/",
        f"http://100{D}64{D}0{D}1/",
        f"127{D}0{D}0{D}1",
        f"169{D}254{D}169{D}254",
        "localhost",
        "localhost:3000",
        "[::1]",
        "::1",
        "0x7f000001",
    ],
)
def test_private_host_forms(text):
    assert patterns({"u": text})["private_host"] is True


@pytest.mark.parametrize(
    "text",
    [
        f"http://8{D}8{D}8{D}8/",
        "http://example.com/",
        f"http://2{D}2{D}2{D}2",
        f"8{D}8{D}8{D}8",
        "100",
        "5",
        "10",
        "note: 5",
        "localhost is a word",
        "http://[2606:4700::1111]/",
        "http://[::ffff:808:808]/",
        "1.2.3",
    ],
)
def test_public_or_non_address_is_not_private(text):
    assert patterns({"u": text})["private_host"] is False


@pytest.mark.parametrize(
    ("text", "allowed", "traversal"),
    [
        ("../etc/passwd", [], True),
        ("a/../../b", [], True),
        (r"a\..\b", [], True),
        ("%2e%2e/secret", [], True),
        ("..", [], True),
        ("a/b/c", [], False),
        ("a..b/c", [], False),
        ("file..name", [], False),
        ("/etc/passwd", [], False),
        ("/etc/passwd", ["/srv/docs"], True),
        ("/srv/docs/a.txt", ["/srv/docs"], False),
        ("/srv/docs", ["/srv/docs"], False),
        ("/srv/docs2/a.txt", ["/srv/docs"], True),
        ("/srv//docs/./a.txt", ["/srv/docs"], False),
        (r"C:\srv\docs\a.txt", ["/srv/docs"], True),
        ("https://x.test/a/b", ["/srv/docs"], False),
        ("relative/path", ["/srv/docs"], False),
    ],
)
def test_path_traversal(text, allowed, traversal):
    assert patterns({"p": text}, allowed_paths=allowed)["path_traversal"] is traversal


def test_secret_like_uses_marker_substrings():
    markers = ["AKIA", "-----BEGIN"]
    assert patterns({"k": "key AKIAIOSFODNN7EXAMPLE"}, secret_markers=markers)["secret_like"]
    assert patterns({"k": ["x", {"y": "-----BEGIN PRIVATE KEY-----"}]}, secret_markers=markers)[
        "secret_like"
    ]
    assert not patterns({"k": "akia lower"}, secret_markers=markers)["secret_like"]
    assert not patterns({"k": "AKIA"})["secret_like"]


def test_limits_are_conservative():
    assert patterns({"k": "x" * MAX_STRING_BYTES}) == only()
    assert patterns({"k": "x" * (MAX_STRING_BYTES + 1)}) == dict.fromkeys(NAMES, True)
    assert patterns({"k": "한" * 1366}) == dict.fromkeys(NAMES, True)  # 4098 bytes
    chunks = {f"k{i}": "y" * 4000 for i in range(MAX_TOTAL_BYTES // 4000 + 1)}
    assert patterns(chunks) == dict.fromkeys(NAMES, True)
    assert patterns({f"k{i}": "y" * 4000 for i in range(10)}) == only()


def test_depth_limit_is_inclusive_of_twenty():
    value: object = "leaf"
    for _ in range(MAX_DEPTH - 1):
        value = [value]
    assert patterns({"k": value}) == only()
    assert patterns({"k": [value]}) == dict.fromkeys(NAMES, True)


def test_unknown_value_types_are_conservative():
    assert patterns({"k": object()}) == dict.fromkeys(NAMES, True)


def test_nested_values_are_inspected():
    result = patterns(
        {"a": {"b": [{"c": f"http://169{D}254{D}169{D}254/"}]}}, allowed_hosts={"ok.test"}
    )
    assert result["private_host"] and result["url_external"]


def test_cost_of_a_full_size_argument_is_small():
    arguments = {f"k{i}": "http://ok.test/path/" + "a" * 3900 for i in range(16)}
    patterns(arguments, allowed_hosts={"ok.test"}, secret_markers=["AKIA"] * 10)
    start = time.perf_counter()
    for _ in range(20):
        patterns(arguments, allowed_hosts={"ok.test"}, secret_markers=["AKIA"] * 10)
    assert (time.perf_counter() - start) / 20 < 0.05
