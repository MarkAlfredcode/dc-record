#!/usr/bin/env python3
"""Gemini (Google AI Studio, free tier) for the cheap-model calls -- added
2026-10-30 to move DC Record's high-volume Haiku work off the Claude
subscription quota (CLAUDE_CODE_OAUTH_TOKEN is shared with interactive use).

DC_BATCH_LLM=gemini routes extract_ja.run_claude (the SAM screen, J&A
extraction, SAM and USAspending triage) here, falling back to Claude Haiku
when every Gemini model fails. gemini_mechanical.py uses it for the OMB/OGE
mechanical passes. The four main passes stay on Claude: a 2026-10-30
Gemini-only dry run of all editions worked, but its main passes added facts
that weren't in the input and called FEC questions "improper". run_pass.py
uses MAIN_MODELS only as a backup when the Claude pass fails (usage limit),
and the edition then carries a disclaimer banner.

Free-tier limits (probed 2026-10-30): Pro models have no free quota (429);
gemini-3.6-flash allows only 20 requests/day; gemini-3.5-flash-lite took
60+ batch calls in one run without a 429. A model that runs out of daily
quota is skipped for the rest of the process.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

API = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
BATCH_MODELS = os.environ.get("DC_GEMINI_BATCH_MODELS", "gemini-3.5-flash-lite,gemini-3.6-flash").split(",")
# Best free model first; gemini-3.8-flash can queue for minutes.
MAIN_MODELS = os.environ.get("DC_GEMINI_MODELS", "gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite").split(",")
# Per-minute limits aren't published per key, so pace request starts
# process-wide and let a 429's retryDelay handle the rest.
RPM = float(os.environ.get("DC_GEMINI_RPM", "10"))
RETRY_WAITS = (0, 20, 60)

_lock = threading.Lock()
_next_slot = [0.0]
_dead = set()  # models out of daily quota this process


def _pace():
    with _lock:
        now = time.time()
        wait = _next_slot[0] - now
        _next_slot[0] = max(now, _next_slot[0]) + 60 / RPM
    if wait > 0:
        time.sleep(wait)


def _retry_delay(body):
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', body)
    return min(float(m.group(1)), 90) if m else 30


def generate(prompt, system, models=BATCH_MODELS, timeout=600, json_mode=True, budget=None):
    """Returns (text, usageMetadata, model). Raises RuntimeError with the
    last few errors if every model fails. `budget` caps the total seconds
    across all retries and models (each request's timeout shrinks to fit)."""
    stop = time.time() + budget if budget else None
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is unset")
    cfg = {"maxOutputTokens": 65536}
    if json_mode:
        cfg["responseMimeType"] = "application/json"
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": cfg,
    }).encode()
    errs = []
    for model in models:
        if model in _dead:
            continue
        for wait in RETRY_WAITS:
            if stop and time.time() + wait + 30 > stop:
                errs.append("time budget spent")
                raise RuntimeError("; ".join(errs[-4:]))
            time.sleep(wait)
            _pace()
            req = urllib.request.Request(API.format(model), data=body,
                                         headers={"x-goog-api-key": key, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=min(timeout, stop - time.time()) if stop else timeout) as r:
                    d = json.load(r)
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                msg = (json.loads(raw or "{}").get("error") or {}).get("message", "") if raw.startswith("{") else raw
                errs.append(f"{model} HTTP {e.code}: {msg[:160]}")
                if e.code == 429 and "PerDay" in raw:
                    _dead.add(model)
                    break
                if e.code == 429:
                    time.sleep(_retry_delay(raw))
                    continue
                if e.code in (500, 502, 503, 504):
                    continue
                break  # 400/403/404: this model won't work, try the next
            except OSError as e:
                errs.append(f"{model} {type(e).__name__}: {e}"[:200])
                if isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError):
                    break  # a model this slow now won't be faster on retry: try the next
                continue
            cand = (d.get("candidates") or [{}])[0]
            parts = (cand.get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            if not text.strip():
                errs.append(f"{model}: empty reply (finishReason={cand.get('finishReason')}, "
                            f"promptFeedback={d.get('promptFeedback')})")
                break
            if cand.get("finishReason") == "MAX_TOKENS":
                errs.append(f"{model}: reply hit maxOutputTokens")
                break
            return text, d.get("usageMetadata") or {}, model
    raise RuntimeError("; ".join(errs[-4:]) or "no Gemini model available")


def envelope(prompt, system, models=BATCH_MODELS, timeout=600, json_mode=True, budget=None):
    """`claude -p --output-format json` lookalike for extract_ja.run_claude's callers."""
    t = time.time()
    text, u, model = generate(prompt, system, models, timeout, json_mode, budget)
    return {
        "type": "result", "is_error": False, "result": text, "total_cost_usd": 0,
        "model": model, "num_turns": 1, "duration_ms": int((time.time() - t) * 1000),
        "usage": {"input_tokens": u.get("promptTokenCount"), "output_tokens": u.get("candidatesTokenCount"),
                  "output_tokens_details": {"thinking_tokens": u.get("thoughtsTokenCount")}},
    }
