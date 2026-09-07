"""Runner: wrap official mini-swe-agent as subprocess with circuit-breaker watchdog."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

FATAL_PATTERNS = [
    "Insufficient Balance",
    "AuthenticationError",
    "invalid_api_key",
    "Incorrect API key",
    "NotFoundError",
    "PermissionDenied",
]
CONSECUTIVE_FATAL_LIMIT = 5
WATCHDOG_INTERVAL = 30


def _exe() -> str:
    return str(Path(sys.executable).parent / "mini-extra")


def _mini_agent_version() -> str:
    import minisweagent
    return minisweagent.__version__


def _mini_config_dir() -> Path:
    import minisweagent
    return Path(minisweagent.__file__).parent / "config"


def build_overlay(run_dir: Path, provider: dict, model: str, gen_kwargs: dict) -> Path:
    """Write the model overlay yaml; base config is the official swebench.yaml."""
    import yaml as _yaml
    overlay = run_dir / "model_overlay.yaml"
    litellm_model = provider["litellm_model"].format(model=model)
    thinking = gen_kwargs.get("thinking")
    model_kwargs = {"drop_params": True}
    if thinking:
        model_kwargs.update(thinking)
    if provider.get("api_base"):
        model_kwargs["api_base"] = provider["api_base"]
    model_cfg = {"model_name": litellm_model, "model_kwargs": model_kwargs}
    if provider.get("cost_tracking") == "ignore_errors":
        model_cfg["cost_tracking"] = "ignore_errors"
    overlay.write_text(_yaml.safe_dump({"model": model_cfg}, sort_keys=False))
    return overlay


def load_exclude(profiles_dir: Path) -> tuple[list[str], dict]:
    import yaml
    data = yaml.safe_load((profiles_dir / "exclude.yaml").read_text())
    ids = list(data.get("instances", {}).keys())
    return ids, data


def scan_done(run_dir: Path) -> set[str]:
    """Instances with a finished traj (any exit status) — resume-safe."""
    done = set()
    if not run_dir.exists():
        return done
    for d in run_dir.iterdir():
        if d.is_dir() and (d / f"{d.name}.traj.json").exists():
            done.add(d.name)
    return done


def prune_failed_preds(run_dir: Path) -> int:
    """Remove preds entries that are empty-patch AND not Submitted (fatal-interrupted).

    Legit 'model made no changes' runs have exit_status Submitted and are kept.
    Returns number pruned.
    """
    preds = run_dir / "preds.json"
    if not preds.exists():
        return 0
    data = json.loads(preds.read_text())
    pruned = []
    for iid, entry in list(data.items()):
        patch = entry.get("model_patch") or ""
        traj = run_dir / iid / f"{iid}.traj.json"
        exit_status = ""
        if traj.exists():
            try:
                exit_status = json.loads(traj.read_text())["info"].get("exit_status", "")
            except Exception:  # noqa: BLE001
                exit_status = ""
        if not patch.strip() and exit_status != "Submitted":
            pruned.append(iid)
            del data[iid]
    if pruned:
        tmp = preds.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(preds)  # atomic
    return len(pruned)


class CircuitBreaker:
    """Watch the runner log + preds.json; trip on fatal API errors without progress.

    Counter resets whenever a new instance completes (preds.json grows).
    """

    def __init__(self, log_path: Path, run_dir: Path, on_trip):
        self.log_path = log_path
        self.run_dir = run_dir
        self.on_trip = on_trip
        self.trip_reason: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._interval = int(os.getenv("SWEVAL_WATCHDOG_INTERVAL", "30"))

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _preds_count(self) -> int:
        preds = self.run_dir / "preds.json"
        try:
            return len(json.loads(preds.read_text()))
        except Exception:  # noqa: BLE001
            return -1

    def _loop(self):
        fatal_re = re.compile("|".join(re.escape(p) for p in FATAL_PATTERNS))
        consecutive = 0
        pos = 0
        last_preds = self._preds_count()
        while not self._stop.wait(self._interval):
            now = self._preds_count()
            if now > last_preds:
                consecutive = 0          # progress happened → reset
                last_preds = now
            if not self.log_path.exists():
                continue
            try:
                with open(self.log_path, errors="replace") as fh:
                    fh.seek(pos)
                    new = fh.read()
                    pos = fh.tell()
            except OSError:
                continue
            for line in new.splitlines():
                if fatal_re.search(line):
                    consecutive += 1
            if consecutive >= CONSECUTIVE_FATAL_LIMIT:
                self.trip_reason = (f"{consecutive} fatal API errors without progress "
                                    f"(preds stuck at {last_preds})")
                self.on_trip(self.trip_reason)
                return


def _runner_env(provider: dict, model: str, base_url: str | None,
                api_key: str) -> dict:
    """Env vars for the mini-swe-agent subprocess (litellm auth)."""
    env = dict(os.environ)
    litellm_model = provider["litellm_model"].format(model=model)
    if provider.get("auth_env"):
        env[provider["auth_env"]] = api_key
    if litellm_model.startswith("openai/"):
        env["OPENAI_API_KEY"] = api_key
        if base_url:
            env["OPENAI_API_BASE"] = base_url
    elif litellm_model.startswith("anthropic/"):
        env["ANTHROPIC_API_KEY"] = api_key
        # api_base is passed via model_kwargs in the overlay (provider["api_base"])
    return env


def run_generation(run_dir: Path, provider: dict, model: str, gen_kwargs: dict,
                   workers: int, exclude: list[str], instances: list[str] | None = None,
                   base_url: str | None = None, api_key: str = "",
                   redo: bool = False, verbose: bool = False) -> dict:
    """Blocking generation run. Returns dict with state + summary."""
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "mini_run.log"
    overlay = build_overlay(run_dir, provider, model, gen_kwargs)
    cfg_dir = _mini_config_dir()

    if prune_failed_preds(run_dir):
        print(f"[sweval] pruned failed empty-patch entries; they will re-run")

    cmd = [
        _exe(), "swebench",
        "--subset", "SWE-bench/SWE-bench_Verified", "--split", "test",
        "-c", str(cfg_dir / "benchmarks" / "swebench.yaml"),
        "-c", str(overlay),
        "-o", str(run_dir),
        "-w", str(workers),
    ]
    if instances:
        alt = "|".join(re.escape(i) for i in instances)
        cmd += ["--filter", f"^({alt})$"]
    elif exclude:
        neg = "|".join(re.escape(i) for i in exclude)
        cmd += ["--filter", f"^(?!({'|'.join(exclude)}))"]
    env = dict(os.environ)
    if provider.get("auth_env") and provider["auth_env"] not in env:
        env["DEEPSEEK_API_KEY"] = env.get("SWEB_API_KEY", "")
    if verbose:
        cmd.append("-v")

    print(f"[sweval] generation start: {' '.join(cmd[:6])} ... -w {workers}")
    print(f"[sweval] log: {log_path}")
    started = time.time()
    env = _runner_env(provider, model, base_url, api_key)
    with open(log_path, "a") as lf:
        lf.write(f"\n=== sweval generation started {time.strftime('%F %T')} ===\n")
        lf.flush()
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env)
    breaker = CircuitBreaker(log_path, run_dir,
                             on_trip=lambda reason: _trip(proc, run_dir, reason))
    breaker.start()
    rc = proc.wait()
    breaker.stop()

    done = scan_done(run_dir)
    elapsed = (time.time() - started) / 60
    state = {
        "returncode": rc,
        "breaker_tripped": breaker.trip_reason,
        "completed": len(done),
        "elapsed_min": round(elapsed, 1),
        "state": "PAUSED" if breaker.trip_reason else ("DONE" if rc == 0 else "FAILED"),
    }
    print(f"[sweval] generation finished: {json.dumps(state)}")
    return state


def _trip(proc: subprocess.Popen, run_dir: Path, reason: str):
    print(f"\n[sweval] CIRCUIT BREAKER TRIPPED: {reason}")
    print(f"[sweval] stopping generation; progress saved under {run_dir}; "
          f"resume with the same command.")
    (run_dir / "PAUSED.txt").write_text(f"{time.strftime('%F %T')} {reason}\n")
    try:
        proc.terminate()
        proc.wait(timeout=30)
    except Exception:  # noqa: BLE001
        proc.kill()
