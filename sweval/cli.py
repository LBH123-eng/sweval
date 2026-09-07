"""sweval CLI: one-command SWE-bench Verified benchmark."""
from __future__ import annotations

import json
import time
from pathlib import Path

import typer
import yaml

from .evaluator import run_evaluation
from .metrics import build_metrics
from .preflight import run_preflight
from .report import write_report
from .runner import _exe, _mini_agent_version, _mini_config_dir, load_exclude, \
    prune_failed_preds, run_generation, scan_done

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="sweval: one-command SWE-bench Verified benchmark")

PROFILES_DIR = Path(__file__).parent / "profiles"
SWEBENCH_VERSION = "5.0.2"


def _resolve_provider(providers: dict, model: str, base_url: str | None) -> dict:
    for name, p in providers.items():
        if not isinstance(p, dict) or "match_base_url" not in p:
            continue
        for alt in p.get("match_model_prefix", []) or []:
            if alt and model.startswith(alt):
                out = dict(p)
                out["name"] = name
                return out
    if not base_url:
        # no endpoint given: match by model-name prefix (e.g. "deepseek-*" -> deepseek)
        for name, p in providers.items():
            if isinstance(p, dict) and model.startswith(name):
                out = dict(p)
                out["name"] = name
                return out
    else:
        # endpoint given: match by base_url substring
        for name, p in providers.items():
            if not isinstance(p, dict) or "match_base_url" not in p:
                continue
            for candidate in p.get("match_base_url", []):
                if base_url and candidate in base_url:
                    out = dict(p)
                    out["name"] = name
                    return out
    p = providers["generic-openai"]
    out = dict(p)
    out["name"] = "generic-openai"
    return out


def _gen_kwargs(provider: dict, tier: str) -> dict:
    thinking = (provider.get("thinking") or {}).get(tier)
    return {"thinking": thinking} if thinking else {}


def _preflight_and_gate(provider: dict, model: str, base_url: str, api_key: str,
                        tier: str, n: int, max_cost: float | None) -> dict:
    gen_kwargs = _gen_kwargs(provider, tier)
    res = run_preflight(provider, model, base_url, api_key, gen_kwargs,
                        n_instances=500 - len(load_exclude(PROFILES_DIR)[0]),
                        max_cost=max_cost)
    for c in res.checks:
        mark = "✅" if c["ok"] else "❌"
        print(f"  {mark} {c['check']}: {c['detail']}")
    if not res.ok:
        print("\n[sweval] preflight FAILED — refusing to start (nothing was spent on generation).")
        raise typer.Exit(2)
    return gen_kwargs


@app.command()
def run(
    model: str = typer.Option(..., help="Model name, e.g. deepseek-v4-flash"),
    api_key: str = typer.Option(..., help="API key"),
    base_url: str = typer.Option(None, help="OpenAI-compatible base URL (auto-detected if omitted)"),
    tier: str = typer.Option("high", help="Reasoning tier: high/medium/off (provider-mapped)"),
    n: int = typer.Option(1, help="Rollouts per instance"),
    workers: int = typer.Option(10, help="Parallel instances"),
    max_cost: float = typer.Option(None, help="Abort if estimated cost exceeds this (USD)"),
    output: Path = typer.Option(None, help="Run directory (default runs/<model>-<ts>)"),
    eval_only: bool = typer.Option(False, help="Skip generation; evaluate existing preds only"),
    instances: str = typer.Option(None, help="Comma-separated instance ids for smoke tests (overrides exclude)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """One command: preflight → generate → evaluate → report."""
    providers = yaml.safe_load((PROFILES_DIR / "providers.yaml").read_text())
    providers = providers.get("providers", providers)
    provider = _resolve_provider(providers, model, base_url)
    base_url = base_url or provider.get("default_base_url")
    exclude, _ = load_exclude(PROFILES_DIR)
    gen_kwargs = _gen_kwargs(provider, tier) if not eval_only else {}
    run_id = output.name if output else f"{model.replace('/', '_')}-{time.strftime('%m%d-%H%M')}"
    run_dir = output or Path("runs") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"[sweval] model={model} provider={provider['name']} tier={tier} n={n}")
    print(f"[sweval] excluded known-broken: {len(exclude)} instances")
    if eval_only:
        _do_eval(run_dir, model, gen_kwargs, n, workers, exclude, verbose)
        return

    gen_kwargs = _preflight_and_gate(provider, model, base_url, api_key, tier,
                                     n, max_cost)
    manifest = {
        "model": model, "provider": provider["name"], "base_url": base_url,
        "tier": tier, "n": n, "workers": workers,
        "gen_kwargs": gen_kwargs,
        "mini_swe_agent_version": _mini_agent_version(),
        "swebench_version": SWEBENCH_VERSION,
        "excluded": exclude,
        "started": time.strftime("%F %T"),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    (run_dir / ".env.api_key").write_text(api_key)  # keep key with the run for resume
    os_chmod = run_dir / ".env.api_key"
    os_chmod.chmod(0o600)

    instance_list = [i.strip() for i in instances.split(",")] if instances else None
    state = run_generation(run_dir, provider, model, gen_kwargs,
                           workers=workers, exclude=exclude, instances=instance_list,
                           base_url=base_url, api_key=api_key, verbose=verbose)
    if state["state"] == "PAUSED":
        print(f"[sweval] generation PAUSED ({state['breaker_tripped']}). "
              f"Fix the issue and re-run the same command to resume.")
        raise typer.Exit(3)
    _do_eval(run_dir, model, gen_kwargs, n, workers, exclude, verbose)


def _do_eval(run_dir: Path, model: str, gen_kwargs: dict, n: int, workers: int,
             exclude: list[str], verbose: bool):
    prune_failed_preds(run_dir)
    done = scan_done(run_dir)
    print(f"[sweval] evaluating {len(done)} instances with official harness "
          f"({SWEBENCH_VERSION})")
    ev = run_evaluation(run_dir, workers=workers)
    if not ev["report_path"]:
        print("[sweval] no report produced; check harness_eval.log")
        raise typer.Exit(4)
    mini_version = _mini_agent_version()
    metrics = build_metrics(run_dir, Path(ev["report_path"]), exclude,
                            mini_version, SWEBENCH_VERSION, model, gen_kwargs, n)
    md = write_report(run_dir, metrics, PROFILES_DIR / "anchors.yaml",
                      PROFILES_DIR / "exclude.yaml")
    print(f"[sweval] REPORT: {md}")
    print(f"[sweval] metrics: {run_dir / 'metrics.json'}")


@app.command()
def resume(
    run_dir: Path = typer.Argument(..., help="Existing run directory"),
    workers: int = typer.Option(10, help="Parallel instances"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Resume an interrupted run (skips finished instances)."""
    manifest = json.loads((run_dir / "manifest.json").read_text())
    (run_dir / "PAUSED.txt").unlink(missing_ok=True)
    provider = _resolve_provider(yaml.safe_load((PROFILES_DIR / "providers.yaml").read_text()).get("providers", {}),
                                 manifest["model"], manifest.get("base_url"))
    api_key = (run_dir / ".env.api_key").read_text().strip()
    gen_kwargs = manifest["gen_kwargs"]
    print(f"[sweval] resuming {run_dir} (model={manifest['model']})")
    state = run_generation(run_dir, provider, manifest["model"], gen_kwargs,
                           workers=workers, exclude=manifest["excluded"],
                           base_url=manifest.get("base_url"), api_key=api_key,
                           verbose=verbose)
    if state["state"] == "PAUSED":
        print(f"[sweval] still blocked: {state['breaker_tripped']}")
        raise typer.Exit(3)
    _do_eval(run_dir, manifest["model"], gen_kwargs, manifest.get("n", 1),
             workers, manifest["excluded"], verbose)


@app.command()
def status(
    run_dir: Path = typer.Argument(..., help="Run directory"),
):
    """Show progress, cost, and state of a run."""
    done = scan_done(run_dir)
    preds = run_dir / "preds.json"
    n_preds = len(json.loads(preds.read_text())) if preds.exists() else 0
    manifest = run_dir / "manifest.json"
    m = json.loads(manifest.read_text()) if manifest.exists() else {}
    paused = run_dir / "PAUSED.txt"
    print(f"run: {run_dir.name}")
    print(f"state: {'PAUSED: ' + paused.read_text().strip() if paused.exists() else 'RUNNING/DONE'}")
    print(f"model: {m.get('model')}  tier: {m.get('tier')}  n: {m.get('n')}")
    print(f"finished trajs: {len(done)} / 500 (preds entries: {n_preds})")
    tok_path = run_dir / "metrics.json"
    if tok_path.exists():
        met = json.loads(tok_path.read_text())
        tok = met.get("tokens", {})
        print(f"cost so far: ${tok.get('cost_usd', 0)}  "
              f"tokens in/out: {tok.get('tokens_in', 0):,}/{tok.get('tokens_out', 0):,}")
    ev_report = list((run_dir / "eval_reports").glob(f"*{run_dir.name}.json")) if (run_dir / "eval_reports").exists() else []
    print(f"evaluation: {'DONE -> ' + str(ev_report[0]) if ev_report else 'pending'}")


if __name__ == "__main__":
    app()
