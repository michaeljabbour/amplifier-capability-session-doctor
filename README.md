# Session Doctor

Understand a broken session before you repair it.

Watch the [70-second film](https://michaeljabbour.github.io/amplifier-skill-session-doctor/#film): a session that will not resume, one tool call with no result, and a repaired copy that says so instead of faking it. The example in it is fictional.

For people whose agent session will not resume. Session Doctor checks the supported transcript structure, names the ambiguity and prepares a separate repair copy when the evidence supports one, leaving the original available.

[Explore the site](https://michaeljabbour.github.io/amplifier-skill-session-doctor/)

## What you get

- **Keep the original intact.** Diagnose first and write any supported repair to a new destination that cannot overwrite existing work.
- **See what is actually missing.** Distinguish a missing tool result from malformed JSON, duplicate IDs or an unsupported event format.
- **Avoid invented history.** A synthetic repair entry says interrupted with no result available; it never pretends an action completed.

## Start here

```sh
git clone https://github.com/michaeljabbour/amplifier-skill-session-doctor.git
cd amplifier-skill-session-doctor
python3 scripts/capability.py install --dest ~/.agents/skills
```

This installs the portable method and its packaged references into your agent skill folder. If your host uses another folder, pass that path to `--dest`. Start a new session or use your host’s skill refresh so it can discover the installed skill. Optional native tools are installed separately.

Try this first task:

> Use Session Doctor to inspect a disposable direct role/content JSONL transcript. Diagnose before changing anything. If a missing tool result is unambiguous, prepare a new repair copy that marks the result unavailable, preserve the original and explain the remaining host-resume check.

## What you need

- Python 3.11 or later for the portable install helper.
- The portable direct-message helper is Python stdlib-only and requires no provider key.
- It reads at most the documented 16 MB limit; unsupported event envelopes are reported explicitly.
- Optional failover, cache and host adapters have separate setup and acceptance requirements.

## Evidence and limits

Native and functional fixtures cover supported structure, ambiguous failures and copy-only repair. They do not establish successful resumption in every host or provider.

The worked examples on the site are fictional. They illustrate the output and decisions; they are not user results or measured benefit. See [consolidation and runtime boundaries](docs/CONSOLIDATION.md) for retained interfaces and acceptance scope.

---

## Retained source guide

### amplifier-bundle-provider-doctor

Provider resilience, session repair, and support tooling for [Amplifier](https://github.com/amplifier-dev/amplifier) agents. Automatically fails over across LLM provider tiers when errors hit, recovers broken sessions from orphaned tool calls and transcript corruption, and provides structured diagnostics and support ticket generation — all as a composable bundle with zero upstream changes.

## The Problem

Amplifier sessions are fragile in three ways:

1. **Provider failures kill sessions.** A 529 "overloaded" from Anthropic crashes the session with no failover to a different model or provider.

2. **Broken sessions are unrecoverable without manual surgery.** Orphaned tool calls, ordering violations, and oversized transcripts require hand-editing `events.jsonl` — a process that takes multiple repair attempts and deep investigation.

3. **No self-service support path.** When things go wrong, diagnosis requires deep ecosystem knowledge. There's no structured way to capture findings and queue support.

## How It Works

The bundle mounts a **FailoverProvider** that implements the Provider protocol and proxies to a priority-ordered chain of real providers. The orchestrator sees ONE provider — failover is invisible.

```
                           ┌─────────────────────────────────┐
                           │        FailoverProvider         │
                           │   (implements Provider protocol) │
                           └──────────────┬──────────────────┘
                                          │
          ┌───────────────────────────────┼───────────────────────────────┐
          │                               │                               │
          ▼                               ▼                               ▼
┌─────────────────────┐            ┌─────────────────────┐            ┌─────────────────────┐
│   Tier 1 (primary)   │            │  Tier 2 (secondary)  │            │  Tier 3 (fallback)  │
│  claude-opus-4-6     │            │  claude-sonnet-4-5   │            │  gpt-5.4            │
│  provider-anthropic  │            │  provider-anthropic  │            │  provider-openai    │
└────────┬────────────┘            └────────┬────────────┘            └────────┬────────────┘
         │                               │                               │
         │  retry_with_backoff()         │  retry_with_backoff()         │  retry_with_backoff()
         │  (handled by provider         │  (handled by provider         │  (handled by provider
         │   internally — 3 retries      │   internally — 3 retries      │   internally — 3 retries
         │   w/ backoff, jitter,         │   w/ backoff, jitter,         │   w/ backoff, jitter,
         │   retry_after headers)        │   retry_after headers)        │   retry_after headers)
         │                               │                               │
         │  on failure ──► fall ──►      │  on failure ──► fall ──►      │  on failure ──► raise
         │                               │                               │
         │  ◄── probe-up ◄── every 5    │  ◄── probe-up ◄── every 5    │
         │       successes               │       successes               │
         └───────────────────────────────┴───────────────────────────────┘
```

### Failover Behavior

- **No retry at this layer.** Each provider already wraps its API calls in `retry_with_backoff()` (3 retries with exponential backoff, jitter, and `retry_after` header support). By the time an error reaches the FailoverProvider, the provider has already exhausted its retry budget. Adding retries here would cause **retry amplification** — each failover-level retry would trigger a fresh round of provider-level retries.
- **Immediate fall to next tier.** On any `LLMError`, the request cascades to the next tier. Non-retryable errors (401 auth, content filter) also fall immediately.
- **Fail-up probing.** After falling to a lower tier, every `probe_interval` successful calls triggers a probe to the tier above. If it succeeds, the provider moves back up. If it fails, the counter resets — no penalty.
- **ContextLengthError.** Special handling — falls to a tier with a *larger* context window if one exists, otherwise raises with an actionable message.

## Installation

### As an app bundle (recommended)

App bundles compose onto every session automatically, regardless of which primary bundle you use:

```bash
# From GitHub
amplifier bundle add git+https://github.com/michaeljabbour/amplifier-skill-session-doctor@main --app

# From a local clone
amplifier bundle add file:///path/to/amplifier-bundle-provider-doctor --app
```

Verify it's registered:

```bash
amplifier bundle list
```

You should see `provider-doctor` with status `app`.

### As a regular bundle

If you only want it for specific projects rather than globally:

```bash
amplifier bundle add git+https://github.com/michaeljabbour/amplifier-skill-session-doctor@main
amplifier bundle use provider-doctor
```

### Design constraints

- **All changes local** — nothing in upstream amplifier repos
- **Survives `amplifier reset`** — lives in `~/dev/`, not in `~/.amplifier/cache/`
- **Composable** — works with any root bundle via `includes:`
- **No kernel or orchestrator changes** — pure bundle-level solution

## Configuration

The failover chain is configured in `behaviors/provider-resilience.yaml`:

```yaml
providers:
  - module: provider-failover
    source: ./modules/provider-failover
    config:
      probe_interval: 5        # successful calls before probing tier above
      tiers:
        - provider: provider-anthropic
          model: claude-opus-4-6
          label: opus
        - provider: provider-anthropic
          model: claude-sonnet-4-5
          label: sonnet
        - provider: provider-openai
          model: gpt-5.4
          label: openai-codex
```

### Configuration Reference

| Parameter | Default | Description |
|-----------|---------|-------------|
| `probe_interval` | `5` | Successful calls on a fallback tier before probing the tier above |
| `tiers[].provider` | — | Provider name (e.g., `provider-anthropic`, `provider-openai`) |
| `tiers[].model` | — | Model identifier for this tier |
| `tiers[].label` | — | Human-readable label used in logging and `name` property |

> **Note:** Provider-level retry (`max_failures`, `retry_base_delay`, backoff, jitter, `retry_after` headers) is handled by each provider internally via `retry_with_backoff()`. This module does not duplicate that logic — it adds **failover across different models/providers**, not redundant retry on the same one.

## Components

### Provider Failover Module

**Path:** `modules/provider-failover/`

A Python module implementing the Provider protocol. Mounts as `failover` in the coordinator's provider registry. The orchestrator calls `complete()` or `stream()` on the FailoverProvider, which transparently handles tier cascading and fail-up probing.

```python
class FailoverProvider:
    """Tier-based LLM provider failover manager."""

    @property
    def name(self) -> str:
        return f"failover({self._current_tier.label})"  # e.g. "failover(opus)"

    async def complete(self, request, **kwargs) -> Any:     # standard completion
    def stream(self, request, **kwargs) -> AsyncGenerator:  # streaming completion
    def get_info(self) -> dict:                             # delegates to active tier
    def parse_tool_calls(self, response) -> list:           # delegates to active tier
```

**Error hierarchy:**

| Error | Retryable | Behavior |
|-------|-----------|----------|
| `ProviderUnavailableError` | Yes | Fall to next tier (provider already retried) |
| `RateLimitError` | Yes | Fall to next tier (provider already retried) |
| `AuthenticationError` | No | Fall to next tier immediately |
| `ContentFilterError` | No | Fall to next tier immediately |
| `ContextLengthError` | No | Fall to tier with larger context window, or raise |

### Session Doctor

**Agent:** `provider-doctor:session-doctor`
**Recipe:** `recipes/session-repair.yaml`

Diagnoses and repairs broken Amplifier sessions — orphaned tool calls, ordering violations, oversized transcripts, duplicate entries, and truncated events. Always creates backups before modification. Defaults to REPAIR (inject synthetic entries) over REWIND (truncate history).

```bash
# Run the recipe
"execute provider-doctor:recipes/session-repair.yaml with session_id=<id>"

# Or invoke the agent directly
"ask session-doctor to check session <id>"
```

### Provider Health

**Agent:** `provider-doctor:provider-health`
**Recipe:** `recipes/provider-diagnosis.yaml`

Tests provider connectivity, authentication, and responsiveness across the failover chain. Sends minimal probe requests, measures latency, classifies errors, and provides actionable recommendations.

```bash
# Run the recipe
"execute provider-doctor:recipes/provider-diagnosis.yaml"

# Or invoke the agent
"check if anthropic is healthy"
```

### Support

**Agent:** `provider-doctor:support`
**Template:** `context/support-template.md`

Generates structured support tickets by gathering error context, running provider and session diagnostics, and filling a standardized template. Saves tickets to `docs/support/YYYY-MM-DD-<topic>.md`.

```bash
"create a support ticket for the overloaded errors I've been seeing"
```

## Project Structure

```
amplifier-bundle-provider-doctor/
├── bundle.md                              # Root bundle declaration
├── behaviors/
│   └── provider-resilience.yaml           # Composable behavior: failover config
├── modules/
│   └── provider-failover/                 # Python module
│       ├── pyproject.toml
│       └── provider_failover/
│           ├── __init__.py                # mount() entry point
│           ├── failover.py                # FailoverProvider class
│           └── errors.py                  # LLMError hierarchy
├── agents/
│   ├── session-doctor.md                  # Diagnose + repair sessions
│   ├── provider-health.md                 # Test provider connectivity
│   └── support.md                         # Generate support tickets
├── recipes/
│   ├── session-repair.yaml                # Automated session diagnosis + repair
│   └── provider-diagnosis.yaml            # Test all providers, report status
├── context/
│   └── support-template.md                # Structured support ticket template
├── tests/                                 # 259 tests (pytest)
└── docs/
    └── plans/                             # Design doc + implementation plan
```

## Testing

The test suite covers all failover behaviors with injectable mocks (no real provider calls, no real delays).

```bash
# Run all tests
cd ~/dev/amplifier-bundle-provider-doctor
python3 -m pytest tests/ -v

# Run a specific test module
python3 -m pytest tests/test_failover.py -v      # multi-tier fallback
python3 -m pytest tests/test_retry.py -v          # immediate failover (no retry at this layer)
python3 -m pytest tests/test_failup.py -v         # fail-up probing
python3 -m pytest tests/test_stream.py -v         # streaming failover
python3 -m pytest tests/test_context_length.py -v # context window handling
python3 -m pytest tests/test_mount.py -v          # mount() wiring
```

### What's Tested

| Test Module | Coverage |
|-------------|----------|
| `test_retry.py` | Immediate failover on any error (no retry at this layer), non-retryable error handling |
| `test_failover.py` | Multi-tier cascade, tier exhaustion, model substitution per tier |
| `test_failup.py` | Probe-up after N successes, failed probe stays, success resets counter |
| `test_stream.py` | Streaming happy path + tier fallback on stream errors |
| `test_context_length.py` | Falls to larger window, raises if none available, skips same-size |
| `test_mount.py` | Coordinator wiring, tier resolution, missing provider handling |
| `test_bundle_yaml.py` | Bundle structure, behavior inclusion, agent declarations |
| `test_session_doctor_agent.py` | Agent definition, metadata, diagnosis steps |
| `test_provider_health_agent.py` | Agent definition, probe methodology |
| `test_session_repair_recipe.py` | Recipe structure, step sequencing, conditional repair |
| `test_provider_diagnosis_recipe.py` | Recipe structure, provider testing steps |
| `test_support_agent.py` | Support workflow, template filling |
| `test_support_template.py` | Template structure, required fields |

## License

MIT

## Contributing

This is a composable Amplifier bundle. To contribute:

1. Clone the repo: `git clone https://github.com/michaeljabbour/amplifier-skill-session-doctor.git`
2. Run the tests: `python3 -m pytest tests/ -v`
3. Make changes following the existing patterns (TDD — write tests first)
4. Ensure all 259+ tests pass before submitting a PR


## Optional diagnostic telemetry

Outbound telemetry is off by default. To opt in, set `SESSION_DOCTOR_TELEMETRY_ENABLED=true`
together with your `SUPABASE_URL` and `SUPABASE_ANON_KEY`. The endpoint must use HTTPS;
HTTP is accepted only for exact `localhost`, `127.0.0.1`, or `::1` development endpoints.
Redirects are refused and authentication headers are never forwarded to another endpoint.
Existing ambient Supabase credentials alone do not enable telemetry.

Telemetry omits raw provider errors, tool errors, policy messages, approval reasons and
other `error_message` text centrally. Enabled structured records still include provider/model
identifiers, configured tier/bundle/tool labels, status/count/timing metadata, OS version and
a stable pseudonymous machine hash. Choose nonprivate configuration labels when opting in;
this feature does not make those metadata anonymous. Local failure handling continues even
when telemetry is disabled or unavailable.
