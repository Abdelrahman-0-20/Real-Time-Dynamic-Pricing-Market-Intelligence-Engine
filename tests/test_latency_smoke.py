"""Latency harness smoke test — keeps the protocol (docs/latency-harness.md) honest.

A tiny run: the full 10k-iteration gate runs via `python benchmarks/latency.py`
and in CI. These tests prove the harness measures, reports, and gates.
"""
from __future__ import annotations

import pathlib

from benchmarks.latency import STAGES, run
from pricing.training.train import train


def _bench_artifacts(tmp_path: pathlib.Path) -> str:
    out = tmp_path / "bench-artifacts"
    train(n=2000, seed=5, out_dir=out, control=False)
    return str(out)


def test_harness_runs_measures_and_gates(tmp_path: pathlib.Path) -> None:
    code, result = run(
        n=150, warmup=15, skus=15, max_p99_ms=50.0,
        model_dir=_bench_artifacts(tmp_path), emit=False,
    )

    assert code == 0, f"in-process p99 should be far under 50ms: {result}"
    assert result["result"] == "PASS"
    for key in ("p50_ms", "p95_ms", "p99_ms", "max_ms", "machine", "git_sha", "scope",
                "model_version", "dictionary_version", "gate_max_p99_ms"):
        assert key in result, f"protocol requires '{key}' in every run"
    assert set(result["stages_p99_ms"]) == set(STAGES)
    assert result["p99_ms"] <= 50.0


def test_failure_names_the_blowing_stage(tmp_path: pathlib.Path) -> None:
    """An impossible gate must fail AND say which stage to blame."""
    code, result = run(
        n=30, warmup=5, skus=5, max_p99_ms=0.000001,
        model_dir=_bench_artifacts(tmp_path), emit=False,
    )
    assert code == 1
    assert result["result"] == "FAIL"
    assert "blame" in result and "p99" in result["blame"]
