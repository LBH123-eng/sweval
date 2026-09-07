"""Evaluator: wrap official swebench harness as subprocess."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


def run_evaluation(run_dir: Path, workers: int = 10, timeout: int | None = None,
                   subset_ids: list[str] | None = None) -> dict:
    """Run official harness on preds.json. Returns {'report_path', 'rc'}.

    If subset_ids is provided, evaluate only those (writes filtered preds file).
    """
    preds = run_dir / "preds.json"
    eval_preds = preds
    if subset_ids is not None:
        data = json.loads(preds.read_text())
        filtered = {k: v for k, v in data.items() if k in set(subset_ids)}
        eval_preds = run_dir / "preds.eval.json"
        eval_preds.write_text(json.dumps(filtered, indent=2))
    report_dir = run_dir / "eval_reports"
    report_dir.mkdir(exist_ok=True)
    run_id = run_dir.name

    cmd = [
        str(Path(sys.executable).parent / "swebench"), "eval",
        "SWE-bench/SWE-bench_Verified", "--split", "test",
        "-p", str(eval_preds),
        "--run-id", run_id,
        "-j", str(workers),
        "--report-dir", str(report_dir),
    ]
    if timeout:
        cmd += ["--timeout", str(timeout)]
    log_path = run_dir / "harness_eval.log"
    print(f"[sweval] evaluation start: {run_id} ({workers} workers), log: {log_path}")
    t0 = time.time()
    with open(log_path, "a") as lf:
        lf.write(f"\n=== sweval evaluation started {time.strftime('%F %T')} ===\n")
        lf.flush()
        rc = subprocess.call(cmd, stdout=lf, stderr=subprocess.STDOUT)
    elapsed = (time.time() - t0) / 60

    report_path = _find_report(report_dir, run_id)
    print(f"[sweval] evaluation finished rc={rc} in {elapsed:.0f} min; "
          f"report={report_path}")
    return {"rc": rc, "report_path": str(report_path) if report_path else None,
            "elapsed_min": round(elapsed, 1)}


def _find_report(report_dir: Path, run_id: str) -> Path | None:
    candidates = sorted(report_dir.glob(f"*{run_id}.json"))
    return candidates[0] if candidates else None
