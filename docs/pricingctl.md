# `pricingctl` — operator CLI specification

**Status:** implemented — run `./pricingctl` from the repo root (or `python -m pricing.ctl.main`). Implemented: `status`, `bench`, `price get|explain|simulate`, `model list`, `trace`, `crawl run|status`, `signals list [--quarantined]`, `eval run`, `config list`. Not yet: `completion`, `model promote|rollback` (needs a remote registry).

**Role:** Developer Tooling Engineer surface for the pricing engine. This is the HITL review interface, the debugging tool, and the CI script target — one tool, three audiences.

## Design principles (the contract)

- **Verb-noun, guessable hierarchy.** You can predict a subcommand you've never run.
- **Global flags mean the same thing everywhere:** `-v/--verbose`, `-q/--quiet`, `--json`, `--no-color`, `--dry-run`, `-h/--help`, `--version`.
- **Dual output:** TTY → colored tables; piped → stable JSON, no ANSI, no spinners. `NO_COLOR` respected.
- **Errors state the fix**, never a raw stack trace (trace behind `-v`).
- **Exit codes are an API** — versioned, stable, distinct per failure class.
- **Startup < 100 ms** on the hot commands (`status`, `price`) — lazy-load subcommands, no network on `--help`, cache credentials.
- **Anything that changes state takes `--dry-run`** and, if destructive, `--force` or a confirm.

## Command surface

```text
pricingctl status                          # system health at a glance
pricingctl status --json                   # stream lag, feature staleness, model version, p99

pricingctl price get   --sku SKU           # current price + provenance
pricingctl price explain --sku SKU         # WHY: features, staleness, contributions, clamps  ← HITL view
pricingctl price simulate --sku SKU --price 119.99   # dry-run: what would the system do?

pricingctl model list                      # registry versions + canary state
pricingctl model promote --version V       # gated: requires eval report (blocking HITL)
pricingctl model rollback --to V --force   # destructive → confirms

pricingctl signals list [--quarantined]    # extracted signals + confidence
pricingctl signals replay --id S           # re-run extraction for one signal/document
pricingctl signals quarantine --id S --reason "..."

pricingctl crawl run --source NAME         # trigger a crawl (dry-run by default)
pricingctl crawl status                    # per-source health, blocked/DLQ counts

pricingctl trace <trace_id>                # full decision/extraction story for one ID
pricingctl eval run [--suite extraction|e2e]   # local eval gate before promotion

pricingctl config get <key> | set <key> <value> | list
pricingctl completion bash|zsh|fish
```

### Example — human vs. pipe

```console
$ pricingctl status
✔ streaming    lag 0.4s          Kafka/Flink healthy
✔ features     98.7% fresh       3 stale > 3×ttl (flagged)
✔ model        xgb-v14  canary   p99 21ms
⚠ extraction   LLM circuit OPEN  5 fails/5min → rule-based fallback active
$ pricingctl status --json | jq -r '.extraction.state'
open
```

### Example — the HITL review (`explain`)

```console
$ pricingctl price explain --sku SKU-4471
SKU-4471   served 119.99   model xgb-v14   degraded: false   trace 8f3a…
  features   demand_rate_1h=42.0   competitor_price=124.50 (stale 41m ⚠)
             signal: competitor_stockout  conf 0.81 (decayed 0.63)
  guardrail  129.90 → 119.99   clamped by margin_floor (min 22%)
  alternatives  unclamped model output 129.90 | band [109.00, 139.00]
  review      [a]pprove  [r]eject  [e]scalate      ← one key, no friction
```

## Exit codes (stable contract — breaking them is a major version)

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | unexpected error (message + fix printed; stack behind `-v`) |
| 2 | usage error (bad flags/args — help shown for that subcommand) |
| 3 | system degraded (e.g. extraction circuit open, features stale) — *the command worked, the system isn't healthy* |
| 4 | not found (unknown SKU / signal / trace_id) |
| 5 | dependency unreachable (feature store, registry down) with fix hint |
| 6 | blocked by policy gate (promotion without eval report; missing `--force`) |

`status` returning 3 (not 1) lets CI distinguish "CLI broken" from "system unhealthy".

## Error message standard

```text
✗ pricingctl model promote --version xgb-v14
  Error: promotion blocked — no eval report for xgb-v14 (exit 6)
    Fix: run `pricingctl eval run --suite e2e` and attach the report,
         or review interactively: `pricingctl model promote --version xgb-v14 --interactive`
```

## Distribution & startup discipline

- Single binary (Go/Rust) or a venv-cached launcher; subcommands lazy-loaded.
- Budget: `status` and `price` cold start **< 100 ms**, enforced by a startup-time assertion in CI (a dependency that adds 300 ms to import fails the build).
- Completions for bash/zsh/fish generated from the command tree; `--version` prints build info.
- Config precedence: **flag > env (`PRICINGCTL_*`) > config file > default**, documented in `pricingctl config list`.
