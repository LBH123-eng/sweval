"""Preflight checks: fail fast before spending money."""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field


@dataclass
class PreflightResult:
    ok: bool
    checks: list[dict] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = ""):
        self.checks.append({"check": name, "ok": ok, "detail": detail})
        if not ok:
            self.ok = False
        return ok


def _litellm_probe(provider: dict, model: str, base_url: str | None,
                   api_key: str, gen_kwargs: dict) -> tuple[bool, str]:
    try:
        import litellm
    except ImportError:
        return False, "litellm not installed"
    try:
        kwargs = {}
        if base_url:
            kwargs["api_base"] = base_url
        resp = litellm.completion(
            model=provider["litellm_model"].format(model=model),
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=16,
            api_key=api_key,
            **kwargs,
        )
        text = (resp.choices[0].message.content or "").strip()
        return True, f"completion ok ({text[:20]!r})"
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        for token in ("Insufficient Balance", "invalid_api_key", "Incorrect API key",
                      "authentication", "Authentication", "does not exist", "not found"):
            if token.lower() in msg.lower():
                return False, msg[:200]
        return False, msg[:200]


def _balance_probe(provider: dict, api_key: str) -> tuple[bool, str]:
    """Optional: provider balance query. Returns (known, detail)."""
    url = provider.get("balance_endpoint")
    if not url:
        return True, "provider has no balance endpoint; skipped"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        path = provider.get("balance_json_path", "")
        value = data
        for part in path.split("."):
            if part:
                value = value.get(part, None) if isinstance(value, dict) else None
        try:
            balance = float(value)
        except (TypeError, ValueError):
            return True, f"balance unreadable: {data}"
        if balance <= 0:
            return False, f"Insufficient Balance: {balance:.2f}"
        return True, f"balance={balance:.2f}"
    except Exception as exc:  # noqa: BLE001
        return True, f"balance endpoint unreachable ({exc}); continuing"


def _toolcall_probe(provider: dict, model: str, base_url: str | None,
                    api_key: str) -> tuple[bool, str]:
    try:
        import litellm
        kwargs = {}
        if base_url:
            kwargs["api_base"] = base_url
        resp = litellm.completion(
            model=provider["litellm_model"].format(model=model),
            messages=[{"role": "user", "content": "List /tmp using the bash tool."}],
            tools=[{"type": "function", "function": {
                "name": "bash", "description": "run bash command",
                "parameters": {"type": "object",
                               "properties": {"command": {"type": "string"}},
                               "required": ["command"]}}}],
            max_tokens=256,
            api_key=api_key,
            **kwargs,
        )
        tc = resp.choices[0].message.tool_calls
        return (True, "tool call ok") if tc else (False, "model returned no tool_calls")
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


def run_preflight(provider: dict, model: str, base_url: str | None, api_key: str,
                  gen_kwargs: dict, n_instances: int, max_cost: float | None) -> PreflightResult:
    res = PreflightResult(ok=True)
    res.add("api_key_present", bool(api_key), "api key provided" if api_key else "missing")
    if not res.ok:
        return res

    ok, detail = _litellm_probe(provider, model, base_url, api_key, gen_kwargs)
    res.add("completion_probe", ok, detail)
    if not ok:
        return res

    known, detail = _balance_probe(provider, api_key)
    res.add("balance", known, detail)
    if not known:
        return res

    if provider.get("auth_style") != "x-api-key" or "anthropic" in provider.get("litellm_model", ""):
        ok, detail = _toolcall_probe(provider, model, base_url, api_key)
        res.add("toolcall_probe", ok, detail)

    # cost estimate gate (rough: use measured avg from prior runs if available, else price table)
    est_out = 10500 * n_instances      # tokens, measured avg on this setup
    est_in = 1_500_000 * n_instances   # tokens, measured avg
    pin = provider.get("price_in_cachemiss")
    pout = provider.get("price_out")
    if pin and pout:
        est_cost = (est_in * pin + est_out * pout) / 1_000_000
        res.add("cost_estimate", True,
                f"~${est_cost:.0f} for {n_instances} instances "
                f"(in={est_in/1e6:.1f}M@${pin}/M, out={est_out/1e6:.1f}M@${pout}/M)")
        if max_cost is not None and est_cost > max_cost:
            res.add("cost_gate", False,
                    f"estimated ${est_cost:.0f} exceeds --max-cost ${max_cost:.0f}; "
                    f"raise --max-cost to proceed")
    else:
        res.add("cost_estimate", True, "no price table for provider; estimate skipped")
    return res
