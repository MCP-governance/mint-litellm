import pytest
from pydantic import ValidationError

from mint_litellm.types.identity import (
    OrgSource,
    OrgStatus,
    Principal,
    PrincipalType,
    Provenance,
    SubjectType,
    classify_principal,
    derive_org,
    is_service_account_marker,
    with_org,
)

KEY = "f" * 64


def classify(**kw):
    base = {
        "instance_id": "inst",
        "key_id": KEY,
        "user_id": None,
        "team_id": None,
        "metadata": None,
        "is_master_key": False,
    }
    return classify_principal(**{**base, **kw})


def test_user_key_is_a_user_principal_with_verified_fields():
    p = classify(user_id="u1", team_id="t1")
    assert p.principal_type is PrincipalType.USER
    assert (p.principal_id, p.user_id, p.key_id, p.team_id) == ("u1", "u1", KEY, "t1")
    assert p.provenance["principal_id"] is Provenance.VERIFIED_BY_LITELLM
    assert p.provenance["team_id"] is Provenance.VERIFIED_BY_LITELLM


def test_team_only_key_is_a_team_key_identified_by_key():
    p = classify(team_id="t1")
    assert p.principal_type is PrincipalType.TEAM_KEY
    assert p.principal_id == KEY and p.user_id is None


def test_service_account_marker_needs_all_three_conditions():
    meta = {"service_account_id": "sa-1"}
    assert is_service_account_marker(None, "t1", meta)
    assert not is_service_account_marker("u1", "t1", meta)
    assert not is_service_account_marker(None, None, meta)
    assert not is_service_account_marker(None, "t1", {})
    assert not is_service_account_marker(None, "t1", None)
    assert classify(team_id="t1", metadata=meta).principal_type is PrincipalType.SERVICE_ACCOUNT
    assert classify(user_id="u1", team_id="t1", metadata=meta).principal_type is PrincipalType.USER


def test_master_key_is_its_own_type():
    p = classify(user_id="default_user_id", is_master_key=True)
    assert p.principal_type is PrincipalType.MASTER_KEY
    assert p.principal_id is None


@pytest.mark.parametrize(
    "kw", [{"key_id": None, "user_id": "u1"}, {"key_id": KEY}, {"key_id": None}]
)
def test_unidentifiable_requests_are_anonymous(kw):
    p = classify(**kw)
    assert p.principal_type is PrincipalType.ANONYMOUS
    assert p.principal_id is None


def test_claimed_end_user_is_marked_as_claimed():
    p = classify(user_id="u1", claimed_end_user_id="someone")
    assert p.claimed_end_user_id == "someone"
    assert p.provenance["claimed_end_user_id"] is Provenance.CLAIMED_BY_CALLER


def test_principal_rejects_inconsistent_fields():
    with pytest.raises(ValidationError):
        Principal(instance_id="i", principal_type=PrincipalType.USER)
    with pytest.raises(ValidationError):
        Principal(instance_id="i", principal_type=PrincipalType.TEAM_KEY, key_id=KEY)
    with pytest.raises(ValidationError):
        Principal(instance_id="i", principal_type=PrincipalType.ANONYMOUS, principal_id="x")
    with pytest.raises(ValidationError):
        Principal(
            instance_id="i",
            principal_type=PrincipalType.USER,
            principal_id="u",
            claimed_end_user_id="x",
        )
    with pytest.raises(ValidationError):
        Principal(instance_id="i", principal_type=PrincipalType.USER, principal_id="u", org_id="o")


ACTIVE = {"orgA": OrgStatus.ACTIVE, "orgB": OrgStatus.ACTIVE, "orgC": OrgStatus.CLOSED}
BINDINGS = {
    (SubjectType.TEAM, "t1"): "orgA",
    (SubjectType.USER, "u1"): "orgB",
    (SubjectType.TEAM, "t-closed"): "orgC",
}


def derive(team_id=None, user_id=None, default=None, bindings=BINDINGS, status=ACTIVE):
    return derive_org(
        team_id=team_id,
        user_id=user_id,
        bindings=bindings,
        default_org_id=default,
        org_status=status,
    )


def test_team_binding_wins_over_the_user_binding():
    d = derive(team_id="t1", user_id="u1")
    assert (d.org_id, d.source) == ("orgA", OrgSource.TEAM)


def test_user_binding_applies_only_without_a_team():
    d = derive(user_id="u1")
    assert (d.org_id, d.source) == ("orgB", OrgSource.USER)


def test_a_team_without_binding_ignores_the_user_binding_and_uses_the_default():
    d = derive(team_id="t-unbound", user_id="u1", default="orgA")
    assert (d.org_id, d.source) == ("orgA", OrgSource.DEFAULT)
    assert derive(team_id="t-unbound", user_id="u1").org_id is None


def test_default_org_applies_when_nothing_is_bound():
    d = derive(user_id="stranger", default="orgB")
    assert (d.org_id, d.source) == ("orgB", OrgSource.DEFAULT)


def test_no_binding_and_no_default_means_no_org():
    d = derive(team_id="t9", user_id="u9")
    assert (d.org_id, d.source) == (None, None)


def test_closed_org_means_no_org_and_does_not_fall_back_to_the_default():
    assert derive(team_id="t-closed", default="orgA").org_id is None
    assert derive(team_id="t9", default="orgC").org_id is None
    assert derive(team_id="t1", status={}).org_id is None


def test_with_org_marks_the_org_as_derived():
    base = classify(user_id="u1", team_id="t1")
    placed = with_org(base, derive(team_id="t1", user_id="u1"))
    assert (placed.org_id, placed.org_source) == ("orgA", OrgSource.TEAM)
    assert placed.provenance["org_id"] is Provenance.DERIVED
    cleared = with_org(placed, derive(team_id="t9"))
    assert cleared.org_id is None and "org_id" not in cleared.provenance
