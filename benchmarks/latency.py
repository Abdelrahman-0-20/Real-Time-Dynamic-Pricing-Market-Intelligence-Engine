"""Latency harness — implements docs/latency-harness.md (protocol written FIRST).

    python benchmarks/latency.py                  # local gate: p99 < 50 ms
    python benchmarks/latency.py --json           # pipe-safe JSON, no ANSI
    python benchmarks/latency.py --max-p99 150    # CI threshold

Measures the in-process code path only (cache -> inference -> guardrails ->
serialize). Never quote its output as production p99 — network hops arrive
with the real stack in step 4. On failure it names the stage that blew the
budget, because "p99 was 63 ms" without "it was inference" is unusable.
"""
from __future__ import annotations

import argparse
import json
import math
import platform
import subprocess
import sys
import time
from pathlib import Path

# `python benchmarks/latency.py` puts benchmarks/ on sys.path, not the repo
# root — bootstrap so the documented command works from any directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pricing.compose import build_service  # noqa: E402
from pricing.training.synthetic import FEATURE_ORDER, generate  # noqa: E402

STAGES = ("snapshot", "inference", "guardrails", "serialize")


def _percentile_ms(sorted_ns: list[int], p: float) -> float:
    """Nearest-rank percentile over raw samples (no smoothing)."""
    if not sorted_ns:
        return 0.0
    idx = max(0, min(len(sorted_ns) - 1, math.ceil(p / 100 * len(sorted_ns)) - 1))
    return sorted_ns[idx] / 1e6


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
            cwd=str(Path(__file__).resolve().parent),
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def prewarm(store, dictionary, clock, skus: int = 100, seed: int = 7) -> list[str]:
    """Fill the store with fresh snapshots for `skus` synthetic SKUs."""
    now = clock.now()
    X, _, _ = generate(n=skus, seed=seed)
    names: list[str] = []
    for i in range(skus):
        sku = f"SKU-{i:05d}"
        names.append(sku)
        entity = f"sku:{sku}"
        for j, name in enumerate(FEATURE_ORDER):
            store.put(
                entity, name, float(X[i, j]),
                event_time=now, ttl_ms=dictionary.specs[name].ttl_ms,
            )
    return names


def run(
    n: int = 10_000,
    warmup: int = 1_000,
    skus: int = 100,
    max_p99_ms: float = 50.0,
    market: str = "EG",
    model_dir: str | None = None,
    emit: bool = True,
) -> tuple[int, dict]:
    service, store, log = build_service(market=market, model_dir=model_dir)
    sku_list = prewarm(store, service.dictionary, service.clock, skus=skus)

    def one(i: int) -> tuple[int, dict]:
        spans: dict = {}
        t0 = time.perf_counter_ns()
        service.price(sku_list[i % len(sku_list)], spans=spans)
        return time.perf_counter_ns() - t0, spans

    for i in range(warmup):
        one(i)

    samples: list[int] = []
    stage_samples: dict[str, list[int]] = {s: [] for s in STAGES}
    for i in range(n):
        elapsed, spans = one(i)
        samples.append(elapsed)
        for stage, value in spans.items():
            stage_samples.setdefault(stage, []).append(value)
    samples.sort()

    stages_p99 = {
        stage: _percentile_ms(sorted(vals), 99)
        for stage, vals in stage_samples.items() if vals
    }
    result = {
        "n": n,
        "warmup": warmup,
        "skus": skus,
        "market": market,
        "model_version": service.model_version,
        "dictionary_version": service.dictionary.version,
        "p50_ms": round(_percentile_ms(samples, 50), 4),
        "p95_ms": round(_percentile_ms(samples, 95), 4),
        "p99_ms": round(_percentile_ms(samples, 99), 4),
        "max_ms": round(_percentile_ms(samples, 100), 4),
        "stages_p99_ms": {k: round(v, 4) for k, v in stages_p99.items()},
        "gate_max_p99_ms": max_p99_ms,
        "machine": platform.node(),
        "processor": platform.processor() or "unknown",
        "git_sha": _git_sha(),
        "scope": "in-process code path only — not production network hops",
    }
    passed = result["p99_ms"] <= max_p99_ms
    result["result"] = "PASS" if passed else "FAIL"
    if not passed and stages_p99:
        blame = max(stages_p99, key=stages_p99.get)
        result["blame"] = f"{blame} (p99 {stages_p99[blame]:.3f} ms)"

    if emit:
        print(
            f"p50 {result['p50_ms']:.3f} ms | p95 {result['p95_ms']:.3f} ms | "
            f"p99 {result['p99_ms']:.3f} ms | max {result['max_ms']:.3f} ms   "
            f"(n={n}, warmup={warmup}, skus={skus})"
        )
        print("stages p99 ms: " + "  ".join(f"{k}={v:.3f}" for k, v in stages_p99.items()))
        print(f"machine {result['machine']} | git {result['git_sha']} | dict {result['dictionary_version']}")
        print(
            f"gate: {result['result']} — p99 {result['p99_ms']:.3f} ms "
            f"{'<=' if passed else '>'} {max_p99_ms} ms"
        )
        if "blame" in result:
            print(f"blame: {result['blame']}")
        print(f"scope: {result['scope']} (docs/latency-harness.md)")

    return (0 if passed else 1), result


def dictionary_version(service) -> str:
    return service.dictionary.version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="benchmarks/latency.py",
        description="p99 latency gate for the in-process pricing path.",
    )
    parser.add_argument("--n", type=int, default=10_000, help="measured iterations (default 10000)")
    parser.add_argument("--warmup", type=int, default=1_000, help="warmup iterations, excluded (default 1000)")
    parser.add_argument("--skus", type=int, default=100, help="distinct SKUs to rotate (default 100)")
    parser.add_argument("--max-p99", type=float, default=50.0, help="p99 gate in ms (default 50; CI uses 150)")
    parser.add_argument("--market", default="EG", help="market policy pack (default EG)")
    parser.add_argument("--model-dir", default=None, help="artifact dir (default ./artifacts)")
    parser.add_argument("--json", action="store_true", help="machine-readable output only (pipe-safe)")
    args = parser.parse_args(argv)

    code, result = run(
        n=args.n, warmup=args.warmup, skus=args.skus,
        max_p99_ms=args.max_p99, market=args.market,
        model_dir=args.model_dir, emit=not args.json,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
