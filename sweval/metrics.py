"""Metrics: parse official harness report + trajectories -> metrics dict."""
from __future__ import annotations

import json
from pathlib import Path


def _report_summary(report_path: Path) -> dict:
    d = json.loads(report_path.read_text())
    ids = list(d.keys()) if "resolved_ids" not in d and isinstance(d, dict) and all(
        isinstance(v, dict) for v in d.values()) else None
    # report formats: {iid: {...}} mapping
    if ids:
        resolved = [i for i in ids if d[i].get("resolved")]
        submitted = ids
        per_iid = d
    else:
        resolved = d.get("resolved_ids", [])
        submitted = d.get("submitted_ids", [])
        per_iid = {}
    out = {
        "submitted": len(submitted),
        "resolved": len(resolved),
        "unresolved": len([i for i in submitted if i not in resolved]),
        "resolved_ids": resolved,
        "unresolved_ids": [i for i in submitted if i not in resolved],
    }
    # aggregate test stats for unresolved
    fails = {}
    for iid in out["unresolved_ids"]:
        t = (per_iid.get(iid, {}) or {}).get("tests_status")
        if t:
            f2p_fail = len(t.get("FAIL_TO_PASS", {}).get("failure", []))
            p2p_fail = len(t.get("PASS_TO_PASS", {}).get("failure", []))
            fails[iid] = {"F2P_fail": f2p_fail, "P2P_fail": p2p_fail}
    out["unresolved_detail"] = fails
    return out


def _traj_stats(run_dir: Path, iids: list[str]) -> dict:
    tot_cost = tot_in = tot_out = n_calls = 0.0
    n = 0
    for iid in iids:
        f = run_dir / iid / f"{iid}.traj.json"
        if not f.exists():
            continue
        try:
            t = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        st = t.get("info", {}).get("model_stats", {})
        tot_cost += st.get("instance_cost", 0) or 0
        n_calls += st.get("api_calls", 0) or 0
        n += 1
        for m in t.get("messages", []):
            resp = (m.get("extra", {}) or {}).get("response")
            usage = (resp or {}).get("usage", {}) if isinstance(resp, dict) else {}
            tot_in += usage.get("prompt_tokens", 0) or 0
            tot_out += usage.get("completion_tokens", 0) or 0
    return {"trajs": n, "api_calls": int(n_calls), "cost_usd": round(tot_cost, 2),
            "tokens_in": int(tot_in), "tokens_out": int(tot_out)}


def _per_repo(resolved_ids: list[str], all_ids: list[str]) -> dict:
    out = {}
    for iid in all_ids:
        repo = iid.split("__")[0]
        r = out.setdefault(repo, {"total": 0, "resolved": 0})
        r["total"] += 1
    for iid in resolved_ids:
        out[iid.split("__")[0]]["resolved"] += 1
    for repo, r in out.items():
        r["rate"] = round(r["resolved"] / r["total"], 4) if r["total"] else 0.0
    return out


def build_metrics(run_dir: Path, report_path: Path | None, exclude: list[str],
                  mini_version: str, swebench_version: str, model: str,
                  gen_kwargs: dict, n: int) -> dict:
    preds = run_dir / "preds.json"
    preds_data = json.loads(preds.read_text()) if preds.exists() else {}
    all_run = list(preds_data.keys())

    metrics = {
        "schema_version": 1,
        "model": model,
        "gen_kwargs": gen_kwargs,
        "n_rollouts": n,
        "mini_swe_agent_version": mini_version,
        "swebench_version": swebench_version,
        "excluded_known_broken": exclude,
    }

    if report_path and Path(report_path).exists():
        summ = _report_summary(Path(report_path))
        resolved = summ["resolved_ids"]
        submitted = summ["submitted"]
        metrics["evaluation"] = {
            "submitted": submitted,
            "resolved": summ["resolved"],
            "unresolved": summ["unresolved"],
            "resolved_rate_raw": round(summ["resolved"] / submitted, 4) if submitted else None,
            "unresolved_ids": summ["unresolved_ids"],
            "unresolved_detail": summ["unresolved_detail"],
        }
        metrics["per_repo"] = _per_repo(resolved, summ["unresolved_ids"] + resolved)
        # adjusted: drop known-broken from denominator (they were excluded from run already)
        metrics["adjusted_denominator"] = submitted
        metrics["resolved_rate_adj"] = metrics["evaluation"]["resolved_rate_raw"]
        metrics["tokens"] = _traj_stats(run_dir, all_run)
    else:
        metrics["evaluation"] = {"status": "not_evaluated"}
        metrics["tokens"] = _traj_stats(run_dir, all_run)
    return metrics
