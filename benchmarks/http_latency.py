"""HTTP latency gate — the FIRST honest "network included" number.

    python benchmarks/http_latency.py                 # local: e2e p99 < 50 ms
    python benchmarks/http_latency.py --json          # pipe-safe
    python benchmarks/http_latency.py --max-p99 150   # CI threshold

Starts uvicorn as a subprocess (real sockets, real auth, real JSON), warms,
then loads it with concurrent async clients. Reports TWO distributions:
  * e2e p50/p95/p99  — client wall time incl. HTTP+serialization (the gate)
  * serve p99        — server-side X-Serve-Ms (serve path only, comparable
                       to the in-process gate in docs/latency-harness.md)
Exit 0 = gate passed, 1 = failed (with the stage named).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import pathlib
import socket
import subprocess
import sys
import time

import httpx

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

API_KEY = "bench-key"


def _percentile_ms(samples_ns: list[int], p: float) -> float:
    if not samples_ns:
        return 0.0
    ordered = sorted(samples_ns)
    index = max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))
    return ordered[index] / 1e6


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_server(port: int, workers: int = 2, log_path: str = "/tmp/pricing-bench-server.log") -> subprocess.Popen:
    env = {**os.environ, "PRICING_API_KEY": API_KEY}
    env.pop("PRICING_API_DEV", None)
    # Server output goes to a FILE, never devnull: the first bench run failed
    # with a bare "HTTP 500" and no traceback because we discarded the log.
    log = open(log_path, "w", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "pricing.api.main:create_app",
         "--factory", "--ws", "wsproto",  # system websockets copy is too old for uvicorn
         "--workers", str(workers),
         "--port", str(port), "--host", "127.0.0.1", "--log-level", "info"],
        cwd=str(_REPO_ROOT), env=env,
        stdout=log, stderr=subprocess.STDOUT,
    )


def run(n: int = 5000, concurrency: int = 8, max_p99_ms: float = 50.0,
        warmup: int = 200, emit: bool = True) -> tuple[int, dict]:
    port = _free_port()
    server = _start_server(port)
    try:
        base = f"http://127.0.0.1:{port}"
        async def boot() -> dict:
            async with httpx.AsyncClient(timeout=5) as client:
                return await _wait_healthy_raw(client, base)
        health = asyncio.run(boot())

        async def warm() -> None:
            headers = {"X-API-Key": API_KEY}
            async with httpx.AsyncClient(timeout=10) as client:
                for i in range(warmup):
                    response = await client.post(
                        f"{base}/v1/price",
                        json={"sku": f"SKU-{i % 100:05d}"}, headers=headers,
                    )
                    if response.status_code != 200:
                        raise RuntimeError(f"warmup failed: {response.status_code}")
        asyncio.run(warm())

        e2e_ns, serve_ms = asyncio.run(_load_fixed(port, n, concurrency))
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    result = {
        "n": len(e2e_ns),
        "concurrency": concurrency,
        "e2e_p50_ms": round(_percentile_ms(e2e_ns, 50), 3),
        "e2e_p95_ms": round(_percentile_ms(e2e_ns, 95), 3),
        "e2e_p99_ms": round(_percentile_ms(e2e_ns, 99), 3),
        "e2e_max_ms": round(_percentile_ms(e2e_ns, 100), 3),
        "serve_p50_ms": round(_percentile_ms(
            [int(m * 1e6) for m in serve_ms], 50), 3) if serve_ms else None,
        "serve_p99_ms": round(sorted(serve_ms)[max(0, math.ceil(0.99 * len(serve_ms)) - 1)], 3)
        if serve_ms else None,
        "health": health,
        "gate_max_p99_ms": max_p99_ms,
        "scope": "localhost HTTP, real auth + JSON + decision log write",
    }
    passed = result["e2e_p99_ms"] <= max_p99_ms
    result["result"] = "PASS" if passed else "FAIL"
    if not passed:
        # Attribute: if serve p99 is small vs e2e, the gap is transport/queueing.
        if result["serve_p99_ms"] is not None and result["serve_p99_ms"] * 3 < result["e2e_p99_ms"]:
            result["blame"] = (f"transport/concurrency (serve p99 "
                               f"{result['serve_p99_ms']}ms vs e2e {result['e2e_p99_ms']}ms) — "
                               "check client concurrency and uvicorn workers")
        else:
            result["blame"] = f"serve path (serve p99 {result['serve_p99_ms']}ms)"

    if emit:
        print(f"e2e p50 {result['e2e_p50_ms']} | p95 {result['e2e_p95_ms']} | "
              f"p99 {result['e2e_p99_ms']} | max {result['e2e_max_ms']} ms   "
              f"(n={result['n']}, concurrency={concurrency})")
        print(f"serve p99 {result['serve_p99_ms']} ms (server-side X-Serve-Ms)")
        print(f"gate: {result['result']} — e2e p99 {result['e2e_p99_ms']} "
              f"{'<=' if passed else '>'} {max_p99_ms} ms")
        if "blame" in result:
            print(f"blame: {result['blame']}")
        print(f"scope: {result['scope']}")
    return (0 if passed else 1), result


async def _wait_healthy_raw(client: httpx.AsyncClient, base: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            response = await client.get(f"{base}/health")
            if response.status_code == 200:
                return response.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
        await asyncio.sleep(0.2)
    raise RuntimeError(f"server never became healthy within {timeout}s ({last})")


async def _load_fixed(port: int, total: int, concurrency: int) -> tuple[list[int], list[float]]:
    e2e: list[int] = []
    serve: list[float] = []
    lock = asyncio.Lock()
    url = f"http://127.0.0.1:{port}/v1/price"
    headers = {"X-API-Key": API_KEY}
    issued = 0

    async def worker(client: httpx.AsyncClient) -> None:
        nonlocal issued
        while True:
            async with lock:
                index = issued
                issued += 1
            if index >= total:
                return
            started = time.perf_counter_ns()
            response = await client.post(
                url, json={"sku": f"SKU-{index % 100:05d}"}, headers=headers
            )
            elapsed = time.perf_counter_ns() - started
            if response.status_code != 200:
                raise RuntimeError(
                    f"request failed: HTTP {response.status_code} {response.text[:200]}"
                )
            async with lock:
                e2e.append(elapsed)
                serve.append(float(response.headers["X-Serve-Ms"]))

    async with httpx.AsyncClient(timeout=15) as client:
        await asyncio.gather(*(worker(client) for _ in range(concurrency)))
    return e2e, serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="benchmarks/http_latency.py",
        description="HTTP serve gate: real uvicorn + auth + JSON, localhost.",
    )
    parser.add_argument("--n", type=int, default=5000, help="measured requests (default 5000)")
    parser.add_argument("--concurrency", type=int, default=8, help="parallel clients (default 8)")
    parser.add_argument("--max-p99", type=float, default=50.0, help="e2e p99 gate in ms (CI: 150)")
    parser.add_argument("--warmup", type=int, default=200, help="warmup requests (default 200)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    try:
        code, result = run(n=args.n, concurrency=args.concurrency,
                           max_p99_ms=args.max_p99, warmup=args.warmup,
                           emit=not args.json)
    except Exception as exc:  # noqa: BLE001 — CLI must not dump stack traces
        print(f"Error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("  Fix: train the model first (`python -m pricing.training.train`) "
              "and check the port is free.", file=sys.stderr)
        return 5
    if args.json:
        print(json.dumps(result, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
