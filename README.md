# sweval — One-command SWE-bench Verified evaluation

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![mini-SWE-agent](https://img.shields.io/badge/agent-mini--SWE--agent%20v2-6f42c1)](https://github.com/SWE-agent/mini-swe-agent)
[![harness](https://img.shields.io/badge/harness-swebench%205.0.2-orange)](https://github.com/SWE-bench/SWE-bench)
[![license](https://img.shields.io/badge/license-MIT-green)](#license)

**Give it a model name and an API key — get a leaderboard-comparable SWE-bench Verified report.**

`sweval` wraps the *official* [mini-SWE-agent v2](https://github.com/SWE-agent/mini-swe-agent)
and the *official* [SWE-bench evaluation harness](https://github.com/SWE-bench/SWE-bench)
into a single CLI. No custom scaffolding, no modified prompts — the numbers it produces
are directly comparable with the [official Verified leaderboard](https://www.swebench.com/verified.html)
(mini-SWE-agent v2 view).

```bash
sweval run --model deepseek-v4-flash --api-key $DEEPSEEK_API_KEY
```

That's it. Preflight checks → 491/500 instances (auto-excludes 9 known-broken) →
official agent loop → official harness grading → `REPORT.md` + `metrics.json`.

## Why

Evaluating coding agents on SWE-bench properly is deceptively hard:

- The official leaderboard uses **one fixed scaffold** (mini-SWE-agent v2) for all models —
  self-built scaffolds produce numbers 15–20pp off the official view
- A small set of Verified instances is **broken in most local environments** (missing
  templates, external-network tests, cgroup CPU misdetection) and silently deflates
  your score
- Long runs die: API balance exhaustion, rate limits, provider outages — and naive
  scripts lose everything

`sweval` handles all three: official-tool alignment by construction, a versioned
known-broken exclusion list with evidence, and crash-safe resume with a circuit breaker.

## Features

- **One command** — preflight (key / balance / tool-call / cost gate) → generation →
  official grading → report
- **Detached evaluation** — grading runs as a session-detached worker: killing the CLI,
  closing the terminal, or losing the SSH session never loses the evaluation
  (progress via `sweval status`, completion marked by `EVAL_DONE.txt`)
- **Leaderboard alignment** — official agent + official harness, machine-verified zero
  config drift outside the model block; version matrix printed in every report
- **Crash-safe** — per-instance artifacts are written atomically and incrementally;
  `sweval resume` continues exactly where it stopped (survives Ctrl-C, OOM, balance
  exhaustion, power loss — worst case loses the in-flight instances)
- **Circuit breaker** — consecutive fatal API errors without progress pause the run
  instead of burning money (tested: balance-exhaustion mid-run resumed losslessly)
- **Timeout retry** — `sweval retry-timeouts` automatically finds instances lost to
  infra timeouts (container wall-clock, exec limits) and re-runs them, then re-grades
- **Known-broken exclusion** — 9 instances with per-instance evidence and revival
  conditions (`profiles/exclude.yaml`); reports show both raw (/500) and adjusted (/491) rates
- **Any provider** — OpenAI-compatible, Anthropic-protocol gateways, and custom
  `/v1` endpoints via litellm; reasoning-tier mapping is explicit and reported
- **Anchored reporting** — every report carries the official leaderboard comparison
  table (2026-02 batch) plus the full reproduction matrix

## Install

```bash
git clone https://github.com/<you>/sweval.git
cd sweval
pip install -e .

# plus the official toolchain (pinned)
pip install mini-swe-agent==2.4.6 swebench==5.0.2

# and a working docker + the SWE-bench instance images
# (each instance has its own image: swebench/sweb.eval.x86_64.<instance_id>:latest)
```

## Quick start

```bash
export DEEPSEEK_API_KEY=sk-xxx

# full run on 491 instances, 10 parallel
sweval run --model deepseek-v4-flash --api-key $DEEPSEEK_API_KEY

# any OpenAI-compatible endpoint
sweval run --model gpt-5.2 --api-key $OPENAI_API_KEY \
           --base-url https://api.openai.com/v1

# smoke test on 5 instances
sweval run --model <model> --api-key $KEY \
           --instances django__django-14672,matplotlib__matplotlib-25122,...

# interrupted? just re-run the same command or:
sweval resume runs/<run_id>

sweval status runs/<run_id>       # progress, cost, breaker state
sweval report runs/<run_id>       # (re)generate REPORT.md
```

### CLI reference

| Command | Purpose |
|---|---|
| `sweval run` | preflight → generate → evaluate → report |
| `sweval resume <run_dir>` | idempotent resume (skips finished instances) |
| `sweval status <run_dir>` | progress / cost / breaker state |
| `sweval retry-timeouts <run_dir>` | re-run instances lost to infra timeouts |
| `sweval report <run_dir>` | (re)generate REPORT.md + metrics.json |

Key `run` options: `--tier high|medium|off` (reasoning mapping), `--n 3` (rollouts),
`--max-cost 50` (cost gate), `--workers 10`, `--instances id1,id2` (smoke subset),
`--output runs/<name>`.

## Output layout

```
runs/<run_id>/
├── manifest.json                 # versions, params, exclusion list (reproduction matrix)
├── preds.json                    # instance_id → model_patch (updated incrementally)
├── mini_run.log                  # generation log (API errors, retries, breaker)
├── <instance_id>/<...>.traj.json # full agent trajectory per instance
├── eval_reports/<...>.json       # official harness report (per-instance test status)
├── harness_eval.log
├── metrics.json                  # machine-readable metrics (schema v1)
└── REPORT.md                     # human-readable report with anchor comparison
```

`REPORT.md` contains: reproduction matrix (agent/harness/dataset versions, reasoning
mapping, n), resolved rate (raw + adjusted), unresolved instances with F2P/P2P
breakdown, per-repo table, cost/tokens, the official anchor comparison, and the
known-broken exclusion list.

## Metrics

| Metric | Meaning |
|---|---|
| `resolved_rate_raw` | resolved / 500 — directly comparable with the official leaderboard |
| `resolved_rate_adj` | resolved / (500 − known-broken) — true capability estimate |
| `pass@k` | with `--n k` rollouts |
| cost / tokens | measured per run, from agent trajectories |

Known-broken instances (currently 9) all fail **with the gold patch too** — verified
causes: external-network tests (sphinx linkcheck/latex, requests httpbin), missing
auth templates (django), cgroup CPU misdetection (pylint), harness timeout
(scikit-learn). Each carries evidence in `profiles/exclude.yaml`.

## How alignment is guaranteed

1. Agent = official mini-SWE-agent **2.4.6** (pinned), launched as a subprocess with the
   official `swebench.yaml` + a model-only overlay; config drift outside the model block
   is machine-checked to be zero
2. Grading = official swebench **5.0.2** harness, stock
3. Official behavior respected: v2 = native tool calling, no temperature override,
   step limit 250, cost limit $3
4. Known differences are declared in every report: reasoning-tier mapping is
   provider-specific (self-mapped), n=1 has ±2–3pp variance, local image digests are
   not bit-verified against Docker Hub (gold-patch 491/499 is the equivalence evidence)

Full methodology: [`PIPELINE_IO.md`](PIPELINE_IO.md) and the alignment report.

## Results on this setup

5-instance smoke, two gateway models (2026-09-07, full I/O walkthrough in
[`PIPELINE_IO.md`](PIPELINE_IO.md)):

| Model | Resolved | Notes |
|---|---|---|
| kernelcat0.1 (→ deepseek-v4-flash-0731) | 4/5 | 260 API calls, 7.25M in / 87.7K out tokens |
| kernelcat0.2-flash (→ glm-5.3-flash) | 4/5 | 74 calls, 0.58M in / 15.1K out |

Both failed only `sympy__sympy-21930`; zero protocol failures.

## Project layout

```
sweval/
├── cli.py            # typer entry: run / resume / status
├── preflight.py      # key, balance, tool-call, cost gate
├── runner.py         # mini-swe-agent subprocess + circuit breaker + resume pruning
├── evaluator.py      # official harness subprocess wrapper
├── metrics.py        # report parsing → metrics (schema v1)
├── report.py         # REPORT.md + metrics.json
└── profiles/
    ├── providers.yaml  # endpoint templates + reasoning-tier maps
    ├── exclude.yaml    # known-broken instances (evidence + revival conditions)
    └── anchors.yaml    # official leaderboard anchors (2026-02 batch)
```

## Roadmap

- [ ] `--n` rollouts with pass@k reporting
- [ ] Multi-model batch queue
- [ ] Automatic failure taxonomy for unresolved instances
- [ ] SWE-rebench subset support (contamination-resistant)
- [ ] Local httpbin for network-dependent eval tests

## License

MIT
