"""Report generation: REPORT.md + metrics.json."""
from __future__ import annotations

import json
import time
from pathlib import Path

import yaml


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def write_report(run_dir: Path, metrics: dict, anchors_path: Path,
                 exclude_path: Path) -> Path:
    m = metrics
    lines: list[str] = []
    ev = m.get("evaluation", {})
    lines.append(f"# SWE-bench Verified 评测报告 — {m['model']}")
    lines.append("")
    lines.append(f"> run_id: `{run_dir.name}` | 生成时间: {time.strftime('%F %T')}")
    lines.append("")
    lines.append("## 复现信息")
    lines.append("")
    lines.append(f"- mini-SWE-agent: **{m['mini_swe_agent_version']}** (官方 2.x, tool calling)")
    lines.append(f"- swebench harness: **{m['swebench_version']}** (官方)")
    lines.append(f"- dataset: princeton-nlp/SWE-bench_Verified (test, 500)")
    lines.append(f"- gen 参数: `{json.dumps(m['gen_kwargs'])}`")
    lines.append(f"- n_rollouts: {m['n_rollouts']} (n=1 波动 ±2-3pp)")
    lines.append(f"- excluded known-broken: {len(m['excluded_known_broken'])} 条")
    lines.append("")

    if ev.get("status") == "not_evaluated":
        lines.append("## 状态：未评分")
        lines.append("")
        tok = m.get("tokens", {})
        lines.append(f"- 生成进度: {tok.get('trajs', 0)} 题轨迹已落盘")
        lines.append(f"- 已花费: ${tok.get('cost_usd', 0)}")
        lines.append("- 评分命令：`swebench eval SWE-bench/SWE-bench_Verified -p preds.json ...`")
    else:
        resolved = ev.get("resolved", 0)
        submitted = ev.get("submitted", 0)
        rate = ev.get("resolved_rate_raw")
        lines.append("## 核心结果")
        lines.append("")
        lines.append(f"- **resolved: {resolved}/{submitted} = {rate * 100:.1f}%**"
                     if rate is not None else f"- resolved: {resolved}/{submitted}")
        tok = m.get("tokens", {})
        if tok:
            lines.append(f"- tokens: in {int(tok.get('tokens_in', 0)):,} / "
                         f"out {int(tok.get('tokens_out', 0)):,}; "
                         f"cost ${tok.get('cost_usd', 0)}; "
                         f"api_calls {tok.get('api_calls', 0):,}")
        lines.append("")
        lines.append("### 未解决任务")
        lines.append("")
        detail = ev.get("unresolved_detail") or {}
        if detail:
            for iid, det in detail.items():
                lines.append(f"- {iid}: F2P fail={det['F2P_fail']}, P2P fail={det['P2P_fail']}")
        else:
            for iid in ev.get("unresolved_ids", []):
                lines.append(f"- {iid}")
        lines.append("### per-repo")
        lines.append("")
        lines.append("| repo | resolved/total | rate |")
        lines.append("|---|---|---|")
        for repo, r in sorted(m.get("per_repo", {}).items()):
            lines.append(f"| {repo} | {r['resolved']}/{r['total']} | {r['rate'] * 100:.0f}% |")

    # anchors comparison
    anchors = _load_yaml(anchors_path).get("entries", [])
    if anchors and ev.get("resolved_rate_raw") is not None:
        rate_pct = ev["resolved_rate_raw"] * 100
        lines.append("")
        lines.append("## 官方锚点对比（mini-SWE-agent v2, 2026-02 批次）")
        lines.append("")
        lines.append("| 模型 | % Resolved | Avg.$ | 差距(vs 本模型) |")
        lines.append("|---|---|---|---|")
        for a in sorted(anchors, key=lambda x: -x["resolved"]):
            gap = rate_pct - a["resolved"]
            lines.append(f"| {a['model']} {a['note']} | {a['resolved']} | "
                         f"${a['avg_cost']} | {gap:+.1f}pp |")
        lines.append("")
        lines.append("*注: 本模型 n/档位与官方条目可能不同, 详见复现信息; 1.x 条目与 All-agents 视图不可比。*")

    excl = _load_yaml(exclude_path).get("instances", {})
    lines.append("")
    lines.append("## known-broken 排除清单（环境缺陷，非模型失败）")
    lines.append("")
    for iid, info in excl.items():
        lines.append(f"- `{iid}`: {info['reason']}")

    md_path = run_dir / "REPORT.md"
    md_path.write_text("\n".join(lines) + "\n")
    (run_dir / "metrics.json").write_text(json.dumps(m, indent=1, ensure_ascii=False))
    return md_path
