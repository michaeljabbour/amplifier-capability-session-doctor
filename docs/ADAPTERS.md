# Native adapter boundaries

The independent helper is `skills/session-doctor/scripts/session_doctor.py`. It diagnoses bounded direct role/content JSONL and can prepare an exclusive new repair copy for unambiguous missing tool results. It does not modify the source, convert unknown event envelopes, bridge roles, reorder ambiguous entries, rewind history or establish actual live provider resumption.

The retained `provider-doctor:session-doctor` agent and `recipes/session-repair.yaml` delegate to that same helper. Diagnosis of unsupported input remains a finding with no automatic mutation. A synthetic error marker states that an interrupted call has no execution result. The old source remains the rollback; a separate host-mediated resume test is a distinct acceptance step.

`modules/provider-failover/` remains an ordinary runtime provider module with model/provider routing semantics. `modules/hooks-telemetry/` remains a host hook for error/repair observations. They are not replaced by a prompt or activated by the portable installer. Their historical configured model IDs are configuration, not evidence of current availability or routing acceptance. Keep native interfaces and consumers until an actual replacement contract is verified.

These adapters live inside the renamed canonical repository. There is no separate old repository identity to archive independently: GitHub source redirects preserve the owner repository. Retiring an adapter requires identifying active bundle references, tests of its replacement interface and an owner-reviewed rollback; deleting the whole canonical repository would remove the replacement too. No active-host or external-consumer census is claimed here.
