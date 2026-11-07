package mint.policy_test

import rego.v1

patterns(overrides) := object.union(
	{"url_external": false, "private_host": false, "path_traversal": false, "secret_like": false},
	overrides,
)

with_patterns(overrides) := object.union(sample, {"arguments": {"keys": [], "sizes": {}, "patterns": patterns(overrides)}})

with_conditions(value, conditions) := object.union(
	value,
	{"approval": object.union(sample.approval, {"conditions": conditions})},
)

test_pol_patterns_all_false_allow if {
	d := evaluate(with_patterns({}))
	d.decision == "Allow"
	d.missing_inputs == []
}

test_pol_without_patterns_is_stage1_input if {
	evaluate(sample).decision == "Allow"
}

test_pol_e07_external_url if {
	d := evaluate(with_patterns({"url_external": true}))
	d.decision == "Block"
	d.policy_ids == ["POL-E-07"]
	d.observable
}

test_pol_e14_private_host if {
	d := evaluate(with_patterns({"private_host": true}))
	d.decision == "Block"
	d.policy_ids == ["POL-E-14"]
	d.observable
}

test_pol_e09_path_traversal if {
	d := evaluate(with_patterns({"path_traversal": true}))
	d.decision == "Block"
	d.policy_ids == ["POL-E-09"]
	d.observable
}

test_pol_secret_like_alone_has_no_rule if {
	evaluate(with_patterns({"secret_like": true})).decision == "Allow"
}

test_pol_e07_and_e14_together_are_sorted if {
	d := evaluate(with_patterns({"url_external": true, "private_host": true}))
	d.policy_ids == ["POL-E-07", "POL-E-14"]
}

test_pol_with_r1_lists_both_and_stays_observable if {
	value := object.union(with_patterns({"url_external": true}), {"approval": {"present": false}})
	d := evaluate(value)
	d.policy_ids == ["POL-E-07", "R1"]
	d.observable
}

test_pol_block_keeps_call_approval_obligation if {
	value := object.union(with_patterns({"url_external": true}), {"settings": {"high_risk_tools": ["read"]}})
	d := evaluate(value)
	d.decision == "Block"
	{"type": "require_call_approval"} in d.obligations
}

test_pol_e08_e15_extra_key_blocked if {
	value := with_conditions(
		object.union(sample, {"arguments": {"keys": ["repo", "hidden"], "sizes": {"repo": 1, "hidden": 1}}}),
		{"allowed_arg_keys": ["repo"]},
	)
	d := evaluate(value)
	d.decision == "Block"
	d.policy_ids == ["POL-E-08", "POL-E-15"]
	d.observable
}

test_pol_e08_e15_subset_allowed if {
	value := with_conditions(
		object.union(sample, {"arguments": {"keys": ["repo"], "sizes": {"repo": 1}}}),
		{"allowed_arg_keys": ["repo", "title"]},
	)
	evaluate(value).decision == "Allow"
}

test_pol_e08_e15_without_allowed_keys_is_not_applied if {
	value := object.union(sample, {"arguments": {"keys": ["anything"], "sizes": {"anything": 1}}})
	evaluate(value).decision == "Allow"
}

test_pol_malformed_patterns_are_missing_inputs if {
	bad := object.union(sample, {"arguments": {"keys": [], "sizes": {}, "patterns": {"url_external": "yes"}}})
	"arguments.patterns" in evaluate(bad).missing_inputs
	partial := object.union(sample, {"arguments": {"keys": [], "sizes": {}, "patterns": {"url_external": true}}})
	"arguments.patterns" in evaluate(partial).missing_inputs
}

test_pol_malformed_allowed_arg_keys_are_missing_inputs if {
	value := with_conditions(sample, {"allowed_arg_keys": "repo"})
	"approval.conditions.allowed_arg_keys" in evaluate(value).missing_inputs
}

test_context_returns_meta_and_markers if {
	context := data.mint.context with data.mint.meta as meta with data.mint.data as {"secret_markers": ["AKIA"]}
	context.bundle == meta
	context.secret_markers == ["AKIA"]
}

test_context_without_markers_is_empty if {
	context := data.mint.context with data.mint.meta as meta with data.mint.data as {}
	context.secret_markers == []
}
