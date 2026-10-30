#!/usr/bin/env python3
"""Gemini version of the mechanical passes (prompt-feds-mechanical.txt,
prompt-officials-mechanical.txt) -- 2026-10-30. Those prompts tell a
`claude -p --allowedTools Read,Write` run to read the candidate file and
Write content-mechanical.json; here the files go in the prompt and this
script writes the reply to content-mechanical.json.

Usage: gemini_mechanical.py <prompt file> <input file>...
Exit 0 only if content-mechanical.json was written; the workflow falls back
to the Claude Haiku pass otherwise.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gemini  # noqa: E402
from run_pass import extract_json  # noqa: E402

SYSTEM = ("You write one section of DC Record, a journalist's daily research digest. Follow the instructions "
          "exactly. You have no tools: every file the instructions mention is included below. Reply with the "
          "content-mechanical.json object only: no prose before or after it, no code fences.")


def main():
    prompt_path, inputs = sys.argv[1], sys.argv[2:]
    with open(prompt_path) as f:
        prompt = f.read()
    prompt += "\n\n=== FILES (already read for you) ===\n"
    for p in inputs:
        try:
            with open(p) as f:
                prompt += f"\n--- {p} ---\n{f.read()}\n"
        except FileNotFoundError:
            prompt += f"\n--- {p} ---\n(file does not exist)\n"
    prompt += ("\nIgnore the instructions about the Write tool and mechanical_written.flag: reply with the "
               "content-mechanical.json object itself.")
    try:
        envl = gemini.envelope(prompt, SYSTEM, timeout=900)
        content = extract_json(envl["result"])
    except Exception as e:
        print(f"gemini mechanical pass failed: {e}", file=sys.stderr)
        return 1
    if not isinstance(content.get("sections"), list) or not content["sections"]:
        print("gemini mechanical reply lacks sections", file=sys.stderr)
        return 1
    with open("content-mechanical.json", "w") as f:
        json.dump(content, f, indent=2)
        f.write("\n")
    head = (content["sections"][0] or {}).get("headline", "")
    with open("mechanical_written.flag", "w") as f:
        f.write(head + "\n")
    print(f"gemini model={envl['model']} wrote content-mechanical.json: {head}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
