# Latency Harness Protocol — decided *before* building

Written first so the benchmark can't be retrofitted to flatter the result. Changing a threshold here requires a note in the commit message explaining why.

## What is being measured

**The in-process serve path:** cache lookup → feature assembly → XGBoost inference → guardrails → decision-record serialization.

**Explicitly NOT measured (yet):** network hops to Kafka/Flink/remote feature store, client round trip. Those arrive with the real stack in step 4. A number from this harness must never be quoted as "production p99" — the honest phrasing is *"code-path p99 on dev hardware"*.

## Method

| Parameter | Value |
|---|---|
| Clock | `time.perf_counter_ns()` |
| Warmup | 1,000 iterations (JIT/cache/allocator warm, excluded from stats) |
| Measured | 10,000 iterations per run, mixed SKU set (≥ 100 SKUs, hot + cold) |
| Percentiles | p50, p95, p99, max — computed from raw samples, no smoothing |
| Isolation | single process, CPU affinity fixed if available; background load noted in output |
| Output | JSON: `{p50, p95, p99, max, n, machine, git_sha, model_version}` — machine + commit recorded with every run so trends are comparable |

## Thresholds (assertion = exit code, not vibes)

| Environment | Gate | Rationale |
|---|---|---|
| Local dev | **p99 < 50 ms** hard fail | the actual SLA |
| Local dev | p95 < 25 ms warn | headroom signal before p99 slips |
| CI (shared runners) | p99 < 75 ms informational, < 150 ms fail | noisier hardware; the hard gate stays local |
| Cache-miss path | reported separately, never averaged with hit path | mixing them hides the miss penalty |

## Failure = a bug, not a flake

A threshold breach must name the stage: the harness instruments sub-spans (cache, inference, guardrails, serialize) and prints the stage whose p99 blew the budget. "p99 was 63 ms" without "it was inference" is an unusable result.

## Reproduction

```bash
python benchmarks/latency.py                 # local gate, exit 0/1
python benchmarks/latency.py --json          # machine-readable (pipe-safe)
python benchmarks/latency.py --stage-cache    # isolate one sub-span
```
