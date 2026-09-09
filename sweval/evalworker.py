"""Detached evaluation worker: runs harness + metrics + report, survives parent death."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .evaluator import run_evaluation
from .metrics import build_metrics
from .report import write_report
from .runner import load_exclude, _mini_agent_version

PROFILES_DIR = Path(__file__).parent / "profiles"
SWEBENCH_VERSION = "5.0.2"


def main(run_dir_str: str, workers: int, timeout: int | None) -> None:
    run_dir = Path(run_dir_str)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    exclude, _ = load_exclude(PROFILES_DIR)

    ev = run_evaluation(run_dir, workers=workers, timeout=timeout)
    if not ev["report_path"]:
        print(f"[sweval-evalworker] evaluation produced no report; see "
              f"{run_dir / 'harness_eval.log'}", flush=True)
        sys.exit(4)

    metrics = build_metrics(
        run_dir, Path(ev["report_path"]), exclude,
        _mini_agent_version(), SWEBENCH_VERSION,
        manifest["model"], manifest.get("gen_kwargs", {}),
        manifest.get("n", 1),
    )
    # adjusted-rate note: instances submitted < 491 means infra timeouts happened
    md = write_report(run_dir, metrics, PROFILES_DIR / "anchors.yaml",
                      PROFILES_DIR / "exclude.yaml")
    (run_dir / "EVAL_DONE.txt").write_text(
        f"{time_str()} evaluation complete: {metrics['evaluation'].get('resolved')}"
        f"/{metrics['evaluation'].get('submitted')}\n")
    print(f"[sweval-evalworker] REPORT: {md}", flush=True)


def time_str() -> str:
    import time
    return time.strftime("%F %T")


if __name__ == "__main__":
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    timeout = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] != "none" else None
    main(sys.argv[1], workers, timeout)
