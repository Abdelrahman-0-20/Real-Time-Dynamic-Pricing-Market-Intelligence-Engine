"""pricingctl implementation — see docs/pricingctl.md for the contract.

DX rules enforced here (developer-tooling skill):
  * argparse subcommands, verb-noun, every leaf has description + examples
  * --json on every leaf: stable, pipe-safe, no ANSI
  * exit codes are an API: 0 ok · 1 unexpected · 2 usage (argparse) ·
    3 degraded · 4 not found · 5 dependency unreachable · 6 policy gate
  * every error names the fix; stack traces never reach a human
  * heavy imports (xgboost, numpy, sklearn) happen INSIDE command bodies:
    `status`/`config` stay file-read-only and fast
  * state lives in $PRICINGCTL_VAR (default ./var), artifacts in
    $PRICINGCTL_ARTIFACTS (default ./artifacts) — flag > env > default
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys

VERSION = "0.1.0"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2
EXIT_DEGRADED, EXIT_NOT_FOUND, EXIT_UNREACHABLE, EXIT_BLOCKED = 3, 4, 5, 6


class CommandError(Exception):
    def __init__(self, code: int, message: str, fix: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fix = fix


def _var_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("PRICINGCTL_VAR") or REPO_ROOT / "var")


def _artifact_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("PRICINGCTL_ARTIFACTS") or REPO_ROOT / "artifacts")


def _config_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("PRICINGCTL_CONFIG") or REPO_ROOT / "config")


def _read_json(path: pathlib.Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _emit(args, payload: dict, human: list[str]) -> int:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, default=str))
    elif not getattr(args, "quiet", False):
        for line in human:
            print(line)
    return payload.get("_exit", EXIT_OK)


def _require_model() -> pathlib.Path:
    model = _artifact_dir() / "xgb-baseline.json"
    if not model.exists():
        raise CommandError(
            EXIT_UNREACHABLE,
            f"model artifact not found at {model}",
            "run `python -m pricing.training.train` from the repo root first",
        )
    return model


def _seed_rows():
    """Deterministic synthetic world shared by price commands (step-3 data)."""
    from pricing.training.synthetic import generate

    if not hasattr(_seed_rows, "_cache"):
        _seed_rows._cache = generate(n=500, seed=11)
    return _seed_rows._cache


def _build_price_service(market: str):
    """Wire the serve path with file-backed decisions (composition root)."""
    from pricing.adapters.clocks import SystemClock
    from pricing.adapters.jsonl_decision_log import JsonlDecisionLog
    from pricing.adapters.memory_features import InMemoryFeatureStore
    from pricing.adapters.xgboost_model import XGBoostModel
    from pricing.app.serve import PriceService
    from pricing.compose import load_dictionary, load_packs
    from pricing.core.policy import resolve_pack

    _require_model()
    packs = load_packs(_config_dir() / "guardrails")
    pack, pack_fallback = resolve_pack(packs, market)
    dictionary = load_dictionary(_config_dir() / "features")
    artifacts = _artifact_dir()
    model = XGBoostModel(
        artifacts / "xgb-baseline.json", artifacts / "feature_order.json",
        "xgb-baseline-v1",
    )
    store = InMemoryFeatureStore()
    log = JsonlDecisionLog(_var_dir() / "decisions.jsonl")
    service = PriceService(
        store=store, dictionary=dictionary, model=model, log=log,
        clock=SystemClock(), pack=pack, pack_fallback=pack_fallback,
        currency="EGP",
    )
    return service, store, dictionary, log


def _seed_sku(store, dictionary, sku: str) -> None:
    """Seed a deterministic synthetic snapshot for `sku` (step-3 world)."""
    X, _, _ = _seed_rows()
    row = int(hashlib.sha256(sku.encode()).hexdigest()[:8], 16) % len(X)
    now = SystemClockNow()
    for j, name in enumerate(dictionary.specs):
        store.put(f"sku:{sku}", name, float(X[row, j]), event_time=now,
                  ttl_ms=dictionary.specs[name].ttl_ms)


def SystemClockNow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


# --------------------------------------------------------------------- status

def cmd_status(args) -> int:
    metrics = _read_json(_artifact_dir() / "metrics.json")
    if metrics is None:
        raise CommandError(
            EXIT_UNREACHABLE,
            f"no model metrics at {_artifact_dir() / 'metrics.json'}",
            "run `python -m pricing.training.train` to train the baseline model",
        )
    dictionary = _read_json(_config_dir() / "features" / "fv-2026.11.json") or {}
    sources = _read_json(_config_dir() / "sources.json") or {"sources": []}
    bench = _read_json(_var_dir() / "last_bench.json")
    intel = _read_json(_var_dir() / "intel_state.json")

    reviewed = sum(1 for s in sources["sources"] if s.get("tos_reviewed"))
    blocked = len(sources["sources"]) - reviewed
    n_features = len(dictionary.get("features", {}))
    critical = sum(1 for f in dictionary.get("features", {}).values() if f.get("critical"))

    warnings: list[str] = []
    if intel is None:
        warnings.append("intel: never crawled (hint: pricingctl crawl run)")
    if bench is None:
        warnings.append("latency: never benchmarked (hint: pricingctl bench)")

    degraded = False
    if intel is not None and (intel.get("quarantined", 0) or intel.get("advisories", 0)):
        degraded = True
        warnings.append(
            f"intel: last crawl quarantined {intel.get('quarantined', 0)} "
            f"candidate(s), {intel.get('advisories', 0)} advisory(ies)"
        )

    payload = {
        "model": {"version": metrics["model_version"], "mean_regret": metrics["mean_regret"],
                  "trained_at": metrics.get("trained_at")},
        "features": {"version": dictionary.get("model_input_version"), "count": n_features,
                     "critical": critical},
        "latency": ({"p99_ms": bench["p99_ms"], "result": bench["result"]}
                    if bench else None),
        "intel": ({"signals": intel.get("signals_written"),
                   "quarantined": intel.get("quarantined"),
                   "advisories": intel.get("advisories"), "ran_at": intel.get("ran_at")}
                  if intel else None),
        "sources": {"total": len(sources["sources"]), "reviewed": reviewed,
                    "refused": blocked},
        "warnings": warnings,
        "_exit": EXIT_DEGRADED if degraded else EXIT_OK,
    }

    human = [
        f"model      {metrics['model_version']}  (regret {metrics['mean_regret']:.2%} vs oracle)",
        f"features   {dictionary.get('model_input_version')}  {n_features} features, {critical} critical",
        (f"latency    p99 {bench['p99_ms']:.3f} ms ({bench['result']})" if bench
         else "latency    not run — fix: pricingctl bench"),
        (f"intel      {intel['signals_written']} signals, {intel['quarantined']} quarantined "
         f"(ran {intel['ran_at'][:19]})" if intel else
         "intel      never crawled — fix: pricingctl crawl run"),
        f"sources    {len(sources['sources'])} configured · {reviewed} reviewed · {blocked} refused",
        f"gate       {'DEGRADED' if degraded else 'OK'}"
        + (f" — {'; '.join(warnings)}" if warnings else ""),
    ]
    return _emit(args, payload, human)


# ---------------------------------------------------------------------- price

def cmd_price_get(args) -> int:
    service, store, _, _ = _build_price_service(args.market)
    _seed_sku(store, service.dictionary, args.sku)
    record = service.price(args.sku)
    payload = dict(record)
    payload["_exit"] = EXIT_DEGRADED if record["degraded"] else EXIT_OK
    clamps = ", ".join(f"{a['rule']} {a['from_price']}→{a['to_price']}"
                       for a in record["guardrail_actions"]) or "none"
    human = [
        f"{record['sku']}  {record['served_price']} {record['currency']}  "
        f"model {record['model_version']}  trigger {record['trigger']}  arm {record['policy_arm']}",
        f"unclamped {record['unclamped_price']}  clamps: {clamps}  "
        f"stale {len(record['stale_features'])}  dropped {len(record['dropped_features'])}  "
        f"degraded {str(record['degraded']).lower()}",
        f"trace {record['trace_id']}  (view: pricingctl trace {record['trace_id']})",
    ]
    return _emit(args, payload, human)


def cmd_price_explain(args) -> int:
    service, store, _, log = _build_price_service(args.market)
    _seed_sku(store, service.dictionary, args.sku)
    record = service.price(args.sku)

    features = {k: (float("nan") if v is None else v)
                for k, v in record["model_input"]["features"].items()}
    explain = getattr(service.model, "explain", None)  # optional adapter capability
    contributions = explain(features) if explain else []

    payload = {
        "decision": record,
        "contributions": [{"feature": f, "contribution": round(c, 4)}
                          for f, c in contributions],
        "_exit": EXIT_DEGRADED if record["degraded"] else EXIT_OK,
    }

    human = [
        f"{record['sku']}   served {record['served_price']} {record['currency']}   "
        f"model {record['model_version']}   degraded: {str(record['degraded']).lower()}   "
        f"trace {record['trace_id']}",
        f"  guardrail  {record['unclamped_price']} → {record['served_price']}  "
        + (", ".join(a["rule"] for a in record["guardrail_actions"]) or "(no clamps)"),
        f"  stale      {', '.join(record['stale_features']) or 'none'}",
        f"  dropped    {', '.join(record['dropped_features']) or 'none'}",
        "  top contributions:",
    ]
    for entry in payload["contributions"][:6]:
        human.append(f"    {entry['contribution']:>8.3f}  {entry['feature']}")
    human.append(f"  pack       {record['policy_pack']['market']} "
                 f"{record['policy_pack']['version']} ({record['policy_pack']['status']})")

    exit_code = _emit(args, payload, human)

    # Interactive review only on a real TTY (pipe-safe by construction).
    if not getattr(args, "json", False) and sys.stdin.isatty() and sys.stdout.isatty():
        try:
            choice = input("  review      [a]pprove  [r]eject  [e]scalate  (enter skips): ").strip().lower()
        except EOFError:
            choice = ""
        if choice in {"a", "r", "e"}:
            _append_review(record, choice)
            print(f"  recorded {choice} for {record['decision_id']}")
    return exit_code


def _append_review(record: dict, choice: str) -> None:
    from datetime import datetime, timezone

    path = _var_dir() / "reviews.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"decision_id": record["decision_id"], "trace_id": record["trace_id"],
             "sku": record["sku"], "choice": choice,
             "at": datetime.now(timezone.utc).isoformat()}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def cmd_price_simulate(args) -> int:
    from pricing.core.guardrails import apply_guardrails
    from pricing.compose import load_dictionary, load_packs
    from pricing.core.policy import resolve_pack

    if args.price <= 0:
        raise CommandError(EXIT_USAGE, f"--price must be positive, got {args.price}",
                           "pass a real proposed price, e.g. --price 119.99")
    packs = load_packs(_config_dir() / "guardrails")
    pack, pack_fallback = resolve_pack(packs, args.market)
    # Simulation is pure guardrail math — no model load, nothing logged.
    dictionary = load_dictionary(_config_dir() / "features")
    X, _, _ = _seed_rows()
    row = int(hashlib.sha256(args.sku.encode()).hexdigest()[:8], 16) % len(X)
    values = {name: float(X[row, j]) for j, name in enumerate(dictionary.specs)}

    floor, ceiling = pack.band(values["base_cost"], values["category_median_price"])
    final, actions = apply_guardrails(
        args.price, last_price=values["price_last"], floor=floor, ceiling=ceiling,
        max_step_pct=pack.max_step_pct, market=pack.market,
    )
    payload = {
        "sku": args.sku, "market": args.market, "proposed": args.price,
        "served": round(final, 4), "band": [round(floor, 2), round(ceiling, 2)],
        "guardrail_actions": [{"rule": a.rule, "from_price": round(a.from_price, 4),
                               "to_price": round(a.to_price, 4)} for a in actions],
        "pack_fallback": pack_fallback, "_exit": EXIT_OK,
    }
    human = [
        f"{args.sku}  proposed {args.price} → served {payload['served']} "
        f"(band [{payload['band'][0]}, {payload['band'][1]}])",
        "clamps: " + (", ".join(f"{a['rule']} {a['from_price']}→{a['to_price']}"
                                for a in payload["guardrail_actions"]) or "none"),
        "(simulation only — nothing was priced or logged)",
    ]
    return _emit(args, payload, human)


# --------------------------------------------------------------- model / trace

def cmd_model_list(args) -> int:
    metrics = _read_json(_artifact_dir() / "metrics.json")
    if metrics is None:
        raise CommandError(EXIT_UNREACHABLE, f"no metrics at {_artifact_dir()}",
                           "run `python -m pricing.training.train`")
    payload = dict(metrics)
    payload["_exit"] = EXIT_OK
    control = metrics.get("control_mean_regret")
    human = [
        f"{metrics['model_version']}  ({metrics['n_samples']} samples, "
        f"trained {metrics.get('trained_at', '?')[:19]})",
        f"  MAE {metrics['mae_price']:.2f}  regret {metrics['mean_regret']:.2%} vs oracle"
        + (f"  control {control:.2%}" if control is not None else ""),
        f"  input contract {metrics['model_input_version']}",
    ]
    return _emit(args, payload, human)


def cmd_trace(args) -> int:
    from pricing.adapters.jsonl_decision_log import JsonlDecisionLog

    log = JsonlDecisionLog(_var_dir() / "decisions.jsonl")
    record = log.find_trace(args.trace_id)
    if record is None:
        raise CommandError(
            EXIT_NOT_FOUND, f"no decision with trace_id '{args.trace_id}'",
            "check the id (`pricingctl price get --sku …` prints one), or note "
            "that var/decisions.jsonl may have been cleared",
        )
    payload = dict(record)
    payload["_exit"] = EXIT_OK
    human = [
        f"{record['sku']}  {record['served_price']} {record['currency']}  "
        f"{record['requested_at']}  trigger {record['trigger']}",
        f"model {record['model_version']}  degraded {str(record['degraded']).lower()}  "
        f"arm {record['policy_arm']}  pack {record['policy_pack']['market']}/{record['policy_pack']['version']}",
        "clamps: " + (", ".join(f"{a['rule']} {a['from_price']}→{a['to_price']}"
                                for a in record["guardrail_actions"]) or "none"),
        f"stale {record['stale_features']}  dropped {record['dropped_features']}",
        f"fingerprint {record['fingerprint']}",
    ]
    return _emit(args, payload, human)


# ------------------------------------------------------------------ crawl/sig

def _load_intel_state() -> dict | None:
    return _read_json(_var_dir() / "intel_state.json")


def cmd_crawl_run(args) -> int:
    from pricing.intel.adapters.html_parser import HtmlTextParser
    from pricing.intel.adapters.http_fetcher import FixtureFetcher, HttpFetcher
    from pricing.intel.adapters.lexicon_extractor import LexiconExtractor
    from pricing.intel.adapters.memory_signal_sink import MemorySignalSink
    from pricing.intel.adapters.tfidf_documents import TfidfDocumentStore
    from pricing.intel.gates import load_signal_validator
    from pricing.intel.pipeline import CrawlPipeline
    from pricing.intel.sources import SourceRegistry
    from pricing.adapters.clocks import SystemClock

    registry = SourceRegistry.from_config(_config_dir() / "sources.json")

    if args.dry_run:
        plan = []
        for source in registry.all():
            if not source.enabled:
                status = "disabled"
            elif not source.tos_reviewed:
                status = "refused_tos_not_reviewed"
            else:
                status = "would_fetch"
            plan.append({"source": source.id, "status": status, "trust": source.trust})
        payload = {"dry_run": True, "plan": plan, "_exit": EXIT_OK}
        human = ["dry run — nothing fetched:"]
        human += [f"  {p['source']:28s} {p['status']}" for p in plan]
        return _emit(args, payload, human)

    sink = MemorySignalSink()
    pipeline = CrawlPipeline(
        registry=registry,
        fetcher=HttpFetcher() if args.http else FixtureFetcher(_config_dir() / "crawl_seed"),
        parser=HtmlTextParser(),
        documents=TfidfDocumentStore(),
        extractor=LexiconExtractor(),
        sink=sink,
        validator=load_signal_validator(REPO_ROOT / "docs" / "contracts" / "market-signal.schema.json"),
        clock=SystemClock(),
        feature_store=None,
    )
    stats = pipeline.run(args.source)

    from datetime import datetime, timezone
    state = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "signals_written": stats["signals_written"],
        "quarantined": stats["quarantined"],
        "advisories": stats["advisories"],
        "sources": stats["sources"],
        "signals": sink.signals,
        "quarantine_records": sink.quarantines,
        "mode": "http" if args.http else "fixture",
    }
    (_var_dir() / "intel_state.json").parent.mkdir(parents=True, exist_ok=True)
    (_var_dir() / "intel_state.json").write_text(json.dumps(state, indent=2) + "\n")

    payload = dict(state)
    payload["_exit"] = EXIT_DEGRADED if (stats["quarantined"] or stats["advisories"]) else EXIT_OK
    human = [
        f"signals {stats['signals_written']} | quarantined {stats['quarantined']} | "
        f"advisories {stats['advisories']} | state → var/intel_state.json",
    ] + [f"  {sid:28s} {res['status']}" for sid, res in stats["sources"].items()]
    return _emit(args, payload, human)


def cmd_crawl_status(args) -> int:
    state = _load_intel_state()
    if state is None:
        raise CommandError(EXIT_DEGRADED, "no crawl has run yet",
                           "run `pricingctl crawl run` (or `--dry-run` to preview sources)")
    degraded = bool(state["quarantined"] or state["advisories"])
    payload = dict(state)
    payload["_exit"] = EXIT_DEGRADED if degraded else EXIT_OK
    human = [
        f"last crawl {state['ran_at'][:19]} ({state['mode']} mode)",
        f"  signals {state['signals_written']}  quarantined {state['quarantined']}  "
        f"advisories {state['advisories']}",
    ] + [f"  {sid:28s} {res['status']}" for sid, res in state["sources"].items()]
    return _emit(args, payload, human)


def cmd_signals_list(args) -> int:
    state = _load_intel_state()
    if state is None:
        raise CommandError(EXIT_DEGRADED, "no crawl has run yet",
                           "run `pricingctl crawl run` first")
    if args.quarantined:
        rows = state.get("quarantine_records", [])
        payload = {"quarantines": rows, "_exit": EXIT_OK}
        human = ([f"{q['doc_id'][:12]}…  {q['reason']}: {q['detail'][:70]}" for q in rows]
                 or ["no quarantined documents"])
    else:
        rows = state.get("signals", [])
        payload = {"signals": rows, "_exit": EXIT_OK}
        human = [
            f"{s['signal_type']:26s} {gates_subject(s):12s} conf {s['confidence']:.2f} "
            f"trust {s['source_trust']:.2f}"
            for s in rows
        ] or ["no signals recorded"]
    return _emit(args, payload, human)


def gates_subject(signal: dict) -> str:
    subject = signal.get("subject", {})
    return str(subject.get("sku") or subject.get("category") or subject.get("competitor"))


# ------------------------------------------------------------------ eval/bench

def cmd_eval_run(args) -> int:
    from pricing.evals.run import main as eval_main

    code = eval_main(
        ["--json"] if args.json else []
    )
    return code


def cmd_bench(args) -> int:
    from benchmarks.latency import run as bench_run

    _require_model()
    code, result = bench_run(
        n=args.n, warmup=max(50, args.n // 10), max_p99_ms=50.0,
        model_dir=str(_artifact_dir()), emit=False,
    )
    from datetime import datetime, timezone
    result["ran_at"] = datetime.now(timezone.utc).isoformat()
    (_var_dir() / "last_bench.json").parent.mkdir(parents=True, exist_ok=True)
    (_var_dir() / "last_bench.json").write_text(json.dumps(result, indent=2) + "\n")

    payload = dict(result)
    payload["_exit"] = code
    human = [
        f"p50 {result['p50_ms']:.3f}  p95 {result['p95_ms']:.3f}  "
        f"p99 {result['p99_ms']:.3f}  max {result['max_ms']:.3f} ms   (n={result['n']})",
        "stages p99: " + "  ".join(f"{k}={v:.3f}" for k, v in result["stages_p99_ms"].items()),
        f"gate: {result['result']} — p99 {result['p99_ms']:.3f} vs {result['gate_max_p99_ms']} ms "
        f"(code path only, git {result['git_sha']})",
    ]
    return _emit(args, payload, human)


def cmd_config_list(args) -> int:
    sources = _read_json(_config_dir() / "sources.json") or {"sources": []}
    packs = sorted((_config_dir() / "guardrails").glob("*.json"))
    dictionary = _read_json(_config_dir() / "features" / "fv-2026.11.json") or {}
    payload = {
        "sources": sources["sources"],
        "guardrail_packs": [json.loads(p.read_text()) for p in packs],
        "feature_dictionary": dictionary,
        "_exit": EXIT_OK,
    }
    human = [
        f"sources      {len(sources['sources'])} "
        f"({sum(1 for s in sources['sources'] if s.get('tos_reviewed'))} ToS-reviewed)",
        f"packs        {', '.join(p.stem for p in packs) or 'none'}",
        f"dictionary   {dictionary.get('model_input_version')} "
        f"({len(dictionary.get('features', {}))} features)",
        "files: config/sources.json · config/guardrails/*.json · config/features/*.json",
    ]
    return _emit(args, payload, human)


# --------------------------------------------------------------------- parser

EXAMPLES = """\
examples:
  pricingctl status                       system health at a glance
  pricingctl status --json | jq .warnings machine-readable
  pricingctl price get --sku SKU-4471     current price + provenance
  pricingctl price explain --sku SKU-4471 WHY: features, clamps, contributions
  pricingctl price simulate --sku SKU-4471 --price 119.99
  pricingctl crawl run --dry-run          preview sources, fetch nothing
  pricingctl trace <trace_id>             full story for one decision
  pricingctl eval run                     extraction quality gate
  pricingctl bench --n 5000               latency gate (p99 < 50 ms)

exit codes: 0 ok · 1 error · 2 usage · 3 degraded · 4 not found · 5 unreachable · 6 blocked
"""


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output (pipe-safe)")
    common.add_argument("-q", "--quiet", action="store_true", help="errors only")

    root = argparse.ArgumentParser(
        prog="pricingctl", description="Operator CLI for the pricing engine.",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    root.add_argument("--version", action="version", version=f"pricingctl {VERSION}")
    sub = root.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("status", parents=[common], help="system health at a glance",
                       description="Model, features, latency, intel, sources — file reads only (fast).")
    p.set_defaults(func=cmd_status)

    pp = sub.add_parser("price", help="get / explain / simulate a price")
    psub = pp.add_subparsers(dest="subcommand", metavar="<subcommand>")
    psub.required = True
    for name, func, extra in (
        ("get", cmd_price_get, []),
        ("explain", cmd_price_explain, []),
        ("simulate", cmd_price_simulate, [("--price", float)]),
    ):
        leaf = psub.add_parser(name, parents=[common], help={
            "get": "current price + provenance",
            "explain": "HITL view: features, staleness, clamps, contributions",
            "simulate": "dry-run: what would the system do with this price",
        }[name])
        leaf.add_argument("--sku", required=True, help="e.g. SKU-4471")
        leaf.add_argument("--market", default="EG", help="policy pack (default EG)")
        if extra:
            flag, cast = extra[0]
            leaf.add_argument(flag, required=True, type=cast, help="proposed price to simulate")
        leaf.set_defaults(func=func)

    p = sub.add_parser("model", help="model registry")
    psub = p.add_subparsers(dest="subcommand", metavar="<subcommand>")
    psub.required = True
    leaf = psub.add_parser("list", parents=[common], help="registry versions + metrics")
    leaf.set_defaults(func=cmd_model_list)

    p = sub.add_parser("trace", parents=[common], help="one decision's full story")
    p.add_argument("trace_id", help="trace id printed by price commands")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("crawl", help="cold-path operations")
    psub = p.add_subparsers(dest="subcommand", metavar="<subcommand>")
    psub.required = True
    leaf = psub.add_parser("run", parents=[common], help="crawl reviewed sources")
    leaf.add_argument("--source", action="append", default=None, metavar="ID",
                      help="only this source (repeatable)")
    leaf.add_argument("--dry-run", action="store_true", help="show the plan, fetch nothing")
    leaf.add_argument("--http", action="store_true", help="real HTTP instead of seed files")
    leaf.set_defaults(func=cmd_crawl_run)
    leaf = psub.add_parser("status", parents=[common], help="last crawl outcome")
    leaf.set_defaults(func=cmd_crawl_status)

    p = sub.add_parser("signals", help="extracted market signals")
    psub = p.add_subparsers(dest="subcommand", metavar="<subcommand>")
    psub.required = True
    leaf = psub.add_parser("list", parents=[common], help="signals from the last crawl")
    leaf.add_argument("--quarantined", action="store_true", help="show rejected candidates instead")
    leaf.set_defaults(func=cmd_signals_list)

    p = sub.add_parser("eval", help="evaluation gates")
    psub = p.add_subparsers(dest="subcommand", metavar="<subcommand>")
    psub.required = True
    leaf = psub.add_parser("run", parents=[common], help="extraction quality gate vs baseline")
    leaf.set_defaults(func=cmd_eval_run)

    p = sub.add_parser("bench", parents=[common], help="latency gate (p99 < 50 ms)")
    p.add_argument("--n", type=int, default=5000, help="iterations (default 5000)")
    p.set_defaults(func=cmd_bench)

    p = sub.add_parser("config", help="configuration")
    psub = p.add_subparsers(dest="subcommand", metavar="<subcommand>")
    psub.required = True
    leaf = psub.add_parser("list", parents=[common], help="sources, packs, dictionary")
    leaf.set_defaults(func=cmd_config_list)

    return root


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)   # argparse exits 2 on usage errors (contract)
    if not hasattr(args, "func"):
        parser.print_help()
        return EXIT_USAGE
    try:
        return args.func(args)
    except CommandError as exc:
        print(f"Error: {exc.message}", file=sys.stderr)
        print(f"  Fix: {exc.fix}", file=sys.stderr)
        return exc.code
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print("  Fix: check the path above exists (config/, artifacts/, var/)", file=sys.stderr)
        return EXIT_UNREACHABLE
    except Exception as exc:  # noqa: BLE001 — CLI must never dump a stack trace
        print(f"Error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("  Fix: re-run with --json for the failing payload, or check "
              "var/ and config/ integrity", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
