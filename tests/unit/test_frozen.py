import copy
import pickle
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from mint_litellm.types.events import Event, EventKind, control_source_key
from mint_litellm.types.frozen import MAX_JSON_DEPTH, FrozenDict, FrozenList, json_value
from mint_litellm.types.identity import Provenance, classify_principal, derive_org, with_org

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ID = UUID("0192f7a0-7c1e-7a10-8000-0123456789ab")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.__setitem__("x", 1),
        lambda d: d.__delitem__("a"),
        lambda d: d.clear(),
        lambda d: d.pop("a"),
        lambda d: d.popitem(),
        lambda d: d.setdefault("x", 1),
        lambda d: d.update({"x": 1}),
        lambda d: d.__ior__({"x": 1}),
    ],
)
def test_frozen_dict_rejects_every_mutation(mutate):
    d = FrozenDict({"a": 1})
    with pytest.raises(TypeError):
        mutate(d)
    assert d == {"a": 1}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda x: x.append(1),
        lambda x: x.extend([1]),
        lambda x: x.insert(0, 1),
        lambda x: x.remove(1),
        lambda x: x.pop(),
        lambda x: x.clear(),
        lambda x: x.sort(),
        lambda x: x.reverse(),
        lambda x: x.__setitem__(0, 2),
        lambda x: x.__delitem__(0),
        lambda x: x.__iadd__([1]),
        lambda x: x.__imul__(2),
    ],
)
def test_frozen_list_rejects_every_mutation(mutate):
    x = FrozenList([1])
    with pytest.raises(TypeError):
        mutate(x)
    assert x == [1]


def test_frozen_collections_copy_and_pickle():
    value = json_value({"a": [1, {"b": 2}]})
    assert copy.deepcopy(value) == value
    assert pickle.loads(pickle.dumps(value)) == value
    assert isinstance(copy.deepcopy(value)["a"], FrozenList)


def test_json_values_are_copied_and_frozen_recursively():
    source = {"a": [1, {"b": 2}], "c": "x", "d": 1.5, "e": True, "f": None}
    frozen = json_value(source)
    source["a"].append(3)
    source["a"][1]["b"] = 9
    assert frozen == {"a": [1, {"b": 2}], "c": "x", "d": 1.5, "e": True, "f": None}
    with pytest.raises(TypeError):
        frozen["a"].append(3)
    with pytest.raises(TypeError):
        frozen["a"][1]["b"] = 9


@pytest.mark.parametrize(
    "bad",
    [
        (1, 2),
        {1, 2},
        ID,
        NOW,
        b"bytes",
        {1: "int key"},
        {"nested": {"k": (1,)}},
        float("nan"),
        float("inf"),
        object(),
    ],
)
def test_non_json_values_are_rejected(bad):
    with pytest.raises(ValueError):
        json_value(bad)


def test_json_nesting_is_bounded():
    deep: object = 1
    for _ in range(MAX_JSON_DEPTH + 2):
        deep = [deep]
    with pytest.raises(ValueError):
        json_value(deep)


def make_event(**override):
    base = {
        "instance_id": "i",
        "inserted_at": NOW,
        "kind": EventKind.APPROVAL_GRANTED,
        "source_key": control_source_key(EventKind.APPROVAL_GRANTED, "a1", 1),
        "actor": "alice",
        "payload": {"k": [1, 2], "n": {"x": "y"}},
    }
    return Event(**{**base, **override})


def test_event_payload_cannot_change_after_creation():
    source = {"k": [1], "n": {"x": "y"}}
    event = make_event(payload=source, subject={"who": "u1"})
    source["k"].append(2)
    assert event.payload == {"k": [1], "n": {"x": "y"}}
    with pytest.raises(TypeError):
        event.payload["k"].append(2)
    with pytest.raises(TypeError):
        event.payload["new"] = 1
    with pytest.raises(TypeError):
        event.subject["who"] = "u2"


def test_event_payload_must_be_json():
    for bad in ({"id": ID}, {"t": (1, 2)}, {"at": NOW}, {"s": {1}}, {"v": float("nan")}):
        with pytest.raises(ValidationError):
            make_event(payload=bad)
    with pytest.raises(ValidationError):
        make_event(subject={"id": ID})


def test_json_round_trip_keeps_frozen_equal_values():
    event = make_event()
    again = Event.from_json_line(event.to_json_line())
    assert again == event
    with pytest.raises(TypeError):
        again.payload["k"].append(3)


def test_principal_provenance_cannot_change():
    principal = classify_principal(
        instance_id="i",
        key_id="k" * 64,
        user_id="u1",
        team_id="t1",
        metadata=None,
        is_master_key=False,
    )
    with pytest.raises(TypeError):
        principal.provenance["org_id"] = Provenance.DERIVED
    placed = with_org(
        principal,
        derive_org(team_id="t1", user_id="u1", bindings={}, default_org_id=None, org_status={}),
    )
    with pytest.raises(TypeError):
        placed.provenance.clear()
