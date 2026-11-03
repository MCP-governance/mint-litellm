package mint.decision

import data.mint.policy
import rego.v1

decision := policy.verdict
policy_ids := policy.decision_ids
obligations := policy.obligations
observable := policy.observable
policy_version_id := data.mint.meta.policy_version_id
bundle_sha256 := data.mint.meta.bundle_sha256
missing_inputs := sort([name | some name; policy.missing[name]])
