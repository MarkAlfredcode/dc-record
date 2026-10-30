#!/usr/bin/env python3
"""One-turn main pass (added 2026-10-12): prompt + the edition's input
bundle go in on stdin, the edition's content JSON comes back as the reply.
No tools, so no second turn.

Why one turn: the multi-turn passes wrote their whole context into the
1-hour prompt cache every turn (billed at 2x the input price) and re-read
it on the next. A pass that reads once and answers once never reads the
cache back, so caching is switched off and the input is billed once at 1x.
Measured on the 2026-10-08..11 Campaigns test bundle (111 filings): the
2-turn Write-tool version cost $2.44, 156k cache-write tokens.

Links: the model writes href="#7" for item [7] of its input (and "#V3",
"#J2" etc. for other numbered rows); expand_refs() swaps in the real URL
from the map the fetch script saved, so long FEC/SAM URLs are never
retyped (fewer output tokens, no mistyped image numbers). An unknown ref is
left as plain text and reported.

Backup (2026-10-30): if the Claude pass fails (usage limit, outage), the
same prompt goes to Gemini's free tier and the content gets a
`model_notice` that render_common shows as a disclaimer banner up top.

Usage: run_pass.py <prompt file> <input file> <content out> [<refs json>]
Writes claude_result.json (the CLI envelope) for the workflow's cost log.
Exit 0 only if a valid content JSON was written.
"""
import json
import os
import re
import shutil
import subprocess
import sys

SYSTEM = ("You write one edition of DC Record, a journalist's daily research digest. Follow the instructions "
          "exactly. Reply with the edition's JSON object only: no prose before or after it, no code fences.")
THINKING = os.environ.get("DC_MAX_THINKING_TOKENS", "12000")
_REF = re.compile(r"""href=(["'])#([A-Z]?\d+)\1""")


def extract_json(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        if not e.msg.startswith("Extra data"):
            raise
        # The 10/14 PM Feds reply (42K chars in) closed its object and kept
        # going. Stray closing brackets are dropped; an object closed too
        # early (', "key": ...' follows) is merged with the rest.
        obj, pos = json.JSONDecoder().raw_decode(text)
        rest = text[pos:].strip()
        if not rest.strip("}] \n\t"):
            return obj
        if rest.startswith(",") and isinstance(obj, dict):
            tail = extract_json("{" + rest[1:])
            if isinstance(tail, dict) and not set(tail) & set(obj):
                return {**obj, **tail}
        raise


def expand_refs(obj, refs, bad):
    if isinstance(obj, str):
        def sub(m):
            url = refs.get(m.group(2))
            if not url:
                bad.add(m.group(2))
                return m.group(0)
            return f'href="{url}"'
        return _REF.sub(sub, obj)
    if isinstance(obj, list):
        return [expand_refs(v, refs, bad) for v in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "url" and isinstance(v, str) and v.startswith("#") and v[1:] in refs:
                out[k] = refs[v[1:]]
            else:
                out[k] = expand_refs(v, refs, bad)
        return out
    return obj


def unlink_bad(obj, bad):
    """<a href="#99">x</a> with an unknown ref -> plain x (never ship a dead link)."""
    if isinstance(obj, str):
        for r in bad:
            obj = re.sub(rf"""<a href=(["'])#{re.escape(r)}\1[^>]*>(.*?)</a>""", r"\2", obj)
        return obj
    if isinstance(obj, list):
        return [unlink_bad(v, bad) for v in obj]
    if isinstance(obj, dict):
        return {k: unlink_bad(v, bad) for k, v in obj.items()}
    return obj


def run_claude(prompt):
    """The one-turn Claude pass. Returns (envelope, None), or (None, why it failed)."""
    env = {**os.environ, "DISABLE_PROMPT_CACHING": "1", "MAX_THINKING_TOKENS": THINKING}
    exe = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    cmd = [exe, "-p", "--tools", "", "--system-prompt", SYSTEM, "--output-format", "json"]
    # Thinking was 39k of 57k output tokens at the default effort on the
    # Campaigns test (MAX_THINKING_TOKENS is ignored by this model).
    cmd += ["--effort", os.environ.get("DC_EFFORT", "medium")]
    if os.environ.get("DC_MODEL"):
        cmd += ["--model", os.environ["DC_MODEL"]]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=1800, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"claude did not run: {e}", file=sys.stderr)
        return None, str(e)
    with open("claude_result.json", "w") as f:
        f.write(proc.stdout or json.dumps({"error": (proc.stderr or "")[:2000]}))
    try:
        envl = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        why = (proc.stderr or proc.stdout or "")[:500]
        print(f"claude exit {proc.returncode}, no JSON envelope: {why}", file=sys.stderr)
        return None, why
    u = envl.get("usage") or {}
    print(f"cost_usd={envl.get('total_cost_usd')}  input_tokens={u.get('input_tokens')}  "
          f"output_tokens={u.get('output_tokens')}  thinking={((u.get('output_tokens_details') or {}).get('thinking_tokens'))}  "
          f"cache_creation_input_tokens={u.get('cache_creation_input_tokens')}  "
          f"duration_ms={envl.get('duration_ms')}  num_turns={envl.get('num_turns')}", file=sys.stderr)
    if envl.get("is_error"):
        why = str(envl.get("result"))[:500]
        print(f"claude error: {why}", file=sys.stderr)
        return None, why
    return envl, None


def run_gemini_backup(prompt, failure):
    """Same prompt on Gemini's free tier (scripts/gemini.py). Returns
    (envelope, banner text) or (None, None). Needs GEMINI_API_KEY; capped at
    DC_GEMINI_BUDGET_SEC (default 1200) so it can't run past the job timeout."""
    if not os.environ.get("GEMINI_API_KEY"):
        print("no GEMINI_API_KEY, no backup pass", file=sys.stderr)
        return None, None
    import gemini
    print("claude failed -- writing this edition with Gemini", file=sys.stderr)
    try:
        # 600s per request: gemini-3.8-flash took 915s on a Congress test
        # (2026-10-30), so a timeout hands over to the faster models.
        envl = gemini.envelope(prompt, SYSTEM, gemini.MAIN_MODELS, timeout=600,
                               budget=float(os.environ.get("DC_GEMINI_BUDGET_SEC", "1200")))
    except RuntimeError as e:
        print(f"gemini backup failed too: {e}", file=sys.stderr)
        return None, None
    with open("claude_result.json", "w") as f:
        json.dump({**envl, "claude_failure": failure}, f)
    print(f"gemini backup model={envl['model']} duration_ms={envl['duration_ms']}", file=sys.stderr)
    why = ("Claude, which normally writes it, hit its usage limit"
           if re.search(r"limit|quota|usage|resets", failure or "", re.I) else "Claude, which normally writes it, was unavailable")
    return envl, (f"This edition was written by Google Gemini because {why}. Gemini is a backup and may be less "
                  "accurate: it can misstate details or add facts that aren't in the records. Check anything "
                  "important against the linked source documents.")


def main():
    prompt_path, input_path, out_path = sys.argv[1:4]
    refs_path = sys.argv[4] if len(sys.argv) > 4 else None
    with open(prompt_path) as f:
        prompt = f.read()
    with open(input_path) as f:
        prompt += "\n" + f.read()
    envl, failure = run_claude(prompt)
    notice = None
    if envl is None:
        # Claude out of usage (or otherwise down): write the edition with
        # Gemini's free tier and say so in a banner (2026-10-30).
        envl, notice = run_gemini_backup(prompt, failure)
        if envl is None:
            return 1
    try:
        content = extract_json(envl.get("result"))
    except (ValueError, json.JSONDecodeError) as e:
        with open(out_path + ".raw.txt", "w") as f:
            f.write(envl.get("result") or "")
        print(f"reply was not valid JSON ({e}); raw reply saved to {out_path}.raw.txt", file=sys.stderr)
        return 1
    if refs_path:
        with open(refs_path) as f:
            refs = json.load(f).get("refs", {})
        bad = set()
        content = expand_refs(content, refs, bad)
        if bad:
            content = unlink_bad(content, bad)
            print(f"WARNING: unknown link refs left as plain text: {sorted(bad)}", file=sys.stderr)
    if not isinstance(content.get("sections"), list) or not content.get("top_story"):
        print("reply JSON lacks top_story/sections", file=sys.stderr)
        return 1
    if notice:
        content["model_notice"] = notice
    with open(out_path, "w") as f:
        json.dump(content, f, indent=2)
        f.write("\n")
    print(f"wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
