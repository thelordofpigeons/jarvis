# The local tier

What this is: an adapter that lets the daemon ask a small language model, running on your own
machine, to classify items and to summarize text that must not leave the machine. The adapter
is code in `jarvisd/local.py`. The model, the server and the GPU are yours to supply.

## Status, stated plainly

- **Tested against a fake server, not against a real model.** The suite
  (`tests/test_local.py`, `tests/test_local_cli.py`) talks to
  `tests/fakes/fake_openai.py`, a small OpenAI-compatible server in a thread. It proves the
  wire format, the contract checks, the state machine and the gate behaviour. It proves
  nothing about how a real model answers.
- **Not benchmarked on real hardware.** No throughput, latency or accuracy number in this
  repository comes from a real llama-server. `bin/bench.ps1` exists to produce them and has
  not been run for this adapter.
- **Off by default.** `[local].enabled = false`. On a fresh checkout `jarvis local status`
  prints `not_installed`, no socket is opened, and the stub router runs as before.
- **Not built:** a job that feeds held items to the local model (the `held_triage` handler of
  design section 15), a registered Task Scheduler entry that starts llama-swap, Darija
  classification quality (`[router].darija_verified` is still false), tool calling through the
  local model, and any Vulkan versus ROCm comparison. `LlamaBackend.summarize` exists and is
  tested, but nothing in the daemon calls it yet, and v1 collectors withhold sensitive files
  without reading them, so today there is nothing to give it.
- **Provenance gap:** a digest job records `router = "stub"` (the configured adapter) even when
  the local model answered. The audit shows which one did: look for `local_request` events.

## How the pieces fit

```
 jarvisd (Python)                                     the owner's machine only
   dispatch.run_gates
     gate 1 tier, deterministic, runs first, never calls a model
     router.classify  ->  LocalRouter  --HTTP-->  llama-swap or llama-server
                          | falls back to StubRouter           (127.0.0.1:8080)
                          v                                     one GGUF model loaded
     gates 2 and 3, then the local state decides where it runs
   LlamaBackend.status()   <- health probe, waits for a server that is down
   LlamaBackend.summarize  <- sensitive text, only if [local].summarize_sensitive = true
```

### The four tier states

| State | Meaning | What dispatch sees |
|---|---|---|
| `not_installed` | `[local].enabled` is false. Nothing is contacted. | `not_installed` |
| `unavailable` | Enabled, but the host is refused, the API key variable is unset, or `/health` fails. | `unavailable` |
| `up` | `/health` answers and recent requests were valid. | `up` |
| `degraded` | `/health` answers but `degrade_after` (default 2) requests in a row failed or broke the contract. One trial request per `retry_cooldown_s` lets it recover. | `unavailable` |

`dispatch` knows three states, so `degraded` is reported to it as `unavailable`. That is the
loud direction: sensitive items stay held, other items go to Claude.

### Failure semantics (spec 4b)

- A down server is waited for, bounded by the queue class `local_wait_s` and capped by
  `[local].max_inline_wait_s` (default 30 s, so a single wait cannot freeze the two-minute
  tick). A wait that expired is not repeated for `retry_cooldown_s`, so a batch pays it once.
  Configuration errors (refused host, missing key) do not wait.
- After the wait, an item with a gate 1 hit is held and flagged degraded. It never falls
  through to Claude. Any other item goes to Claude through the normal gates.
- Known limit: dispatch sets `GateResult.degraded` only after all three gates pass. The
  fallback router has confidence 0, so these items leave at gate 3 and carry
  `local_tier: unavailable` instead of `degraded: true`. The tier is recorded in the
  `gate_decision` audit events, in the job and in the digest header. It is not silent, but the
  digest's "degraded" line is not what shows it.
- A reply that is not valid JSON, breaks the seven-field contract, or is cut off becomes
  confidence 0 and a `router_invalid` audit event. It never becomes a guess.

### Trust asymmetry (spec 4d)

The router model may propose tools in `needs_tools`. Only names in
`[trust].local_model_allowlist` survive, and anything also listed in `[trust].always_confirm`
is removed even if someone added it to the allowlist. Dropped names are counted in a
`local_proposal_denied` audit event; the names themselves are not logged. The model can add
sensitivity (`sensitive: true` holds the item) and can never remove it. The deterministic
importance rules (financial, client-facing, irreversible, production, work items) are a floor:
a model that rates such an item low is overruled.

### Network rules

The client builds one URL shape, `http://127.0.0.1:<port>`. Any other host is refused before a
socket is created, including `localhost`. The proxy environment is ignored. Redirects are not
followed. The API key is read from the environment variable named in `[local].api_key_env`,
sent as a bearer token and never written anywhere.

## Install on Windows with an AMD GPU

Written for a Radeon card on Windows 11, using the Vulkan backend. Nothing here is automated
and nothing was run by the author of this adapter; follow it with the checks in each step.

1. **Driver.** Install the current AMD Adrenalin driver and reboot. Pin it once it works and
   turn off automatic driver updates: Vulkan device-lost resets under long loads are
   documented upstream for recent RDNA3 drivers.
2. **Pick a folder outside any synced folder**, for example `%LOCALAPPDATA%\jarvis-llm\`, with
   subfolders `bin` and `models`.
3. **llama.cpp, Vulkan build.** From the project's GitHub releases
   (`ggml-org/llama.cpp`) download the Windows Vulkan zip (the asset name follows
   `llama-<build>-bin-win-vulkan-x64.zip`). Unzip it into `bin`. Use a build that includes the
   GGUF parser fixes for CVE-2025-53630 and CVE-2026-27940 (audit step 5 below).
4. **Check the GPU is visible:** `bin\llama-server.exe --list-devices`. If an integrated GPU is
   listed first, set `GGML_VK_VISIBLE_DEVICES` to the discrete card's index before starting the
   server, or the model may load on the wrong device.
5. **llama-swap.** From `mostlygeek/llama-swap` releases download the Windows amd64 zip
   (`llama-swap_<version>_windows_amd64.zip`) and unzip `llama-swap.exe` into `bin`.
   llama-swap starts and stops llama-server per requested model name.
6. **Get a model** through the audit below. For the router, the design names
   `gemma-3-270m` first and `smollm3-3b` if the small model cannot hold the schema. Both are
   unverified here.
7. **llama-swap config**, `bin\config.yaml` (adjust paths; the model key is what
   `[local].router_model` or `[router].model` must say):

   ```yaml
   healthCheckTimeout: 120
   models:
     "gemma-3-270m":
       cmd: >
         C:\path\to\bin\llama-server.exe
         --host 127.0.0.1 --port ${PORT}
         -m C:\path\to\models\gemma-3-270m.gguf
         -ngl 999 --ctx-size 8192
   ```

   `${PORT}` is filled in by llama-swap. Keep the server on loopback.
8. **Start it:** `bin\llama-swap.exe --config bin\config.yaml --listen 127.0.0.1:8080`. Open
   `http://127.0.0.1:8080/health` in a browser; it should answer. A request for a model loads
   it on first use, which can take 10 to 40 seconds for a large file.
9. **Optional API key.** Set a user environment variable, for example `JARVIS_LLAMA_KEY`, and
   put its name in `[local].api_key_env`. Whether your llama-swap version enforces or passes
   through a key is something to verify against its README; the key path in this adapter is
   tested only against the fake server.
10. **Configure the daemon** in `jarvis.local.toml` (never in the tracked file):

    ```toml
    [local]
    enabled = true
    backend = "llama"
    router_model = "gemma-3-270m"
    request_timeout_s = 90          # the first request pays the model load
    ```

    The address comes from `[llama].host` and `[llama].port` in `jarvis.toml`.
11. **Verify before trusting:** `jarvis local check` (works even while `enabled = false`), then
    `jarvis local status`, then `jarvis run-digest --dry-run` and read the held list. Do not
    run `run-digest --claude` as a test of this.

### Keeping it alive

The Phase 0 watchdog (`bin/watchdog.py`) already probes `http://127.0.0.1:8080/health` and
restarts the scheduled task named in `[llama].swap_scheduled_task`. That task is not created
by this repository. Registering one that runs the llama-swap command from step 8, plus the
daily restart at `[llama].daily_restart`, is a manual step that has not been done or tested.

## `jarvis local status` and `jarvis local check`

`jarvis local status [--json]` prints the tier state, the reason, the server address, which
router is in use and whether sensitive summaries are on. It probes `/health` once, and only
when the tier is enabled.

`jarvis local check [--json]` runs the conformance probes against the configured server,
whether or not the tier is enabled, and exits 1 if any fail:

| Probe | Passes when |
|---|---|
| `host` | the configured host is `127.0.0.1` |
| `health` | `GET /health` answers 2xx |
| `router_contract_en`, `_fr`, `_darija` | the reply to a synthetic item is the seven-field contract |
| `plain_completion` | a plain chat completion returns text |

These check the shape of the answers. They use synthetic text only, never vault content. A pass
says nothing about accuracy or speed.

## Model audit, steps 1 to 8

Run per download, before the file is loaded by the daemon. `[audit]` in `jarvis.toml` records
the policy (`require_sha256`, `require_license_record`, `allowed_formats`, `block_pickle`), and
`logs/audit.jsonl` is reserved for the results. No script performs these steps; they are a
checklist done by hand.

1. **Source.** Download only from the publisher's verified organization on Hugging Face. Check
   for a namespace look-alike before you click.
2. **License.** Read it, record it, and confirm it permits your use.
3. **Format.** GGUF or safetensors only. Pickle-based files are refused. Scanners such as
   ModelScan and Fickling are filters, not guarantees.
4. **SHA-256.** Compute the hash of the file and compare it with the model card or a mirror.
   Record it.
5. **Runtime patched.** The llama.cpp build must include the fixes for the GGUF parser CVEs
   (CVE-2025-53630, CVE-2026-27940). Check the release notes for the build you installed.
6. **First load inside a sandbox.** Load the model first under Anthropic's sandbox-runtime
   (`srt`). Note that srt does not currently fence DNS on Windows and that this project's
   Windows Filtering Platform verification defect is still open (design section 16), so this
   step is a precaution, not a proof.
7. **Sanity evaluation.** Run the lm-evaluation-harness sanity suite and confirm the model
   behaves like its card.
8. **Adversarial scan before any tool access.** Run garak against the served endpoint. The
   local model has no tools in this adapter (proposals are filtered, nothing executes them),
   but run this before that ever changes.

The research notes also list step 9 (inspect_ai on the daemon's own scenarios, promptfoo for
regression) and step 10 (mcp-scan on every MCP server). They are not part of the gate for this
adapter.

## Benchmarking: `bin/bench.ps1`

`bin/bench.ps1` wraps `llama-bench` and appends one JSON line per context size to
`logs/bench.jsonl`: prompt-processing and generation tokens per second, wall time, the GPU name
and driver, and a status. A device-lost error at a context size is recorded as the finding and
stops the run.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File bin\bench.ps1 `
  -ModelPath C:\path\to\models\gemma-3-270m.gguf `
  -LlamaBench C:\path\to\bin\llama-bench.exe `
  -ContextSizes 4096,16384,32768
```

Exit codes: 0 done, 4 `llama-bench` not found, 5 model file not found. The script never writes
outside `logs/`. Until someone runs it and commits nothing but the conclusions, treat every
speed or context claim in the design notes as arithmetic from model configs, not measurement.

## Configuration reference

All keys are optional; defaults are shown. `[local]` is strict: an unknown key is an error.

| Key | Default | Meaning |
|---|---|---|
| `[local].enabled` | `false` | Master switch. |
| `[local].backend` | `""` | Must be `"llama"` when enabled. Anything else reads as `unavailable`. |
| `[local].api_key_env` | `""` | Name of the environment variable that holds the bearer token. |
| `[local].router_model` | `""` | Model name sent to the server. Empty falls back to `[router].model`. |
| `[local].summary_model` | `""` | Model for summaries. Empty falls back to `router_model`. |
| `[local].summarize_sensitive` | `false` | Lets the local model read sensitive items. The text never leaves loopback. |
| `[local].health_timeout_s` | `2.0` | Per health probe. |
| `[local].request_timeout_s` | `30.0` | Per router request. |
| `[local].summary_timeout_s` | `120.0` | Per summary request. |
| `[local].max_inline_wait_s` | `30` | Cap on one wait for a down server. |
| `[local].retry_cooldown_s` | `60` | Pause before waiting or asking again after a failure. |
| `[local].degrade_after` | `2` | Bad replies in a row before the tier is `degraded`. |
| `[local].max_input_chars` | `4000` | Item text sent to the router model. |
| `[llama].host`, `[llama].port` | `127.0.0.1`, `8080` | Server address; only `127.0.0.1` is accepted. |
| `[trust].local_model_allowlist` | from `jarvis.toml` | Tools the local model may propose. Empty when missing. |
| `[trust].always_confirm` | from `jarvis.toml` | Actions the local model can never propose. |

## Audit events this adds

All carry ids, codes, counts and milliseconds. None carries item text, a model reply or a key.

| Event | When |
|---|---|
| `local_tier` | The state changed (`from`, `to`, `reason`). |
| `local_request` | One router request (`ok`, `code`, `ms`, `item_id`). |
| `router_invalid` | The model's reply broke the contract (`code`: json, not_object, schema, truncated). |
| `local_proposal_denied` | Proposed tools were dropped (`count`, sanitized `names`). |
| `local_wait_expired` | A wait for the server ran out. |
| `local_summarize` | A local summary ran (`items`, `ok`, byte counts). |
| `local_refused` | A summary was refused because `summarize_sensitive` is off. |
| `local_check` | `jarvis local check` ran (`passed`, `failed`). |

No new Claude call exists in this adapter, so no new budget ledger or breaker use is needed.
