---
name: session-doctor
description: "Diagnose a broken agent session and prepare a reversible repair without inventing execution results. Use when asked to a session cannot resume or provider errors suggest structural corruption."
---

# Session Doctor

Diagnose before changing session state. Identify the exact transcript format, revision, error class and provider context. Do not print private transcript contents or credentials in a support report.

For direct role/content JSONL, scripts/session_doctor.py SOURCE reads at most 16 MB and checks tool-call/result pairing, duplicate IDs, malformed lines and advisory ordering/size concerns. Unknown event envelopes are reported unsupported, not silently interpreted as healthy. Consecutive roles are an advisory because provider formats differ.

Repair: scripts/session_doctor.py SOURCE --repair-copy NEWFILE only fills missing tool results when the structure is otherwise unambiguous. It labels synthetic entries as interrupted/error with no result available, preserves the original bytes and refuses to overwrite a destination. It does not claim the tool ran. Malformed JSON, duplicate IDs and delayed real results require review. No automatic rewind or loss of history.

Verify the new copy structurally and separately test actual host/provider resumption when authorized. A clean structural report is not proof of provider acceptance. For other native event formats, use the host's documented adapter after inspecting the schema; do not feed it to the direct-message helper and pretend the formats match.

Provider diagnosis and cache identity checks use the retained optional provider-failover/telemetry modules and repository agents. A cache mismatch does not justify a force-push or broad cache deletion. Prepare an isolated cache copy and source comparison, then apply only the authorized repair. Escalation contains exact error types and redacted version/configuration evidence, not full private logs.
