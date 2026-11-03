package mint.decision

import rego.v1
import data.mint.policy

decision := policy.verdict
policy_ids := sort([id | some id; policy.applied[id]])
obligations := policy.obligations
observable := policy.observable
policy_version_id := data.mint.meta.policy_version_id
bundle_sha256 := data.mint.meta.bundle_sha256
missing_inputs := sort([name | some name; policy.missing[name]])
