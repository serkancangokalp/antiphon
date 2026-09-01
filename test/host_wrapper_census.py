#!/usr/bin/env python3
"""Count host-owned opening wrappers without retaining transcript content."""

import argparse
import collections
import glob
import json
import os
import re


OPENING_TAG = re.compile(r"^\s*<([A-Za-z][A-Za-z0-9_-]*)(?=[\s>/])")
ABSENT = "<absent>"


def opening_tag(text):
    """Return a complete opening tag name at the start of text, if present."""
    match = OPENING_TAG.match(text or "")
    return match.group(1) if match else None


def _prompt_source(record):
    source = record.get("promptSource", ABSENT)
    return source if isinstance(source, str) else "<non-string>"


def claude_user_blocks(record):
    """Claude user text blocks paired with their host provenance field."""
    if not isinstance(record, dict) or record.get("type") != "user":
        return []
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    source = _prompt_source(record)
    if isinstance(content, str):
        return [(content, source)]
    if not isinstance(content, list):
        return []
    return [(block.get("text"), source) for block in content
            if isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str)]


def codex_user_blocks(record):
    """One joined Codex user message; Codex exposes no prompt-source field."""
    if not isinstance(record, dict) or record.get("type") != "response_item":
        return []
    payload = record.get("payload")
    if (not isinstance(payload, dict) or payload.get("type") != "message"
            or payload.get("role") != "user"):
        return []
    content = payload.get("content")
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [block.get("text") for block in content
                 if isinstance(block, dict)
                 and block.get("type") in ("text", "input_text")
                 and isinstance(block.get("text"), str)]
    else:
        parts = []
    return [("\n".join(parts), ABSENT)] if parts else []


def _side_census(root, blocks_for):
    files = sorted(glob.glob(os.path.join(root, "**", "*.jsonl"),
                             recursive=True))
    malformed = 0
    blocks = 0
    prompt_sources = collections.Counter()
    tags = collections.defaultdict(collections.Counter)
    for path in files:
        try:
            source = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with source:
            for line in source:
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    malformed += 1
                    continue
                for text, prompt_source in blocks_for(record):
                    blocks += 1
                    prompt_sources[prompt_source] += 1
                    tag = opening_tag(text)
                    if tag is not None:
                        tags[tag][prompt_source] += 1
    return {
        "files": len(files),
        "malformed_lines": malformed,
        "user_blocks": blocks,
        "prompt_sources": dict(sorted(prompt_sources.items())),
        "tags": {tag: dict(sorted(sources.items()))
                 for tag, sources in sorted(tags.items())},
    }


def census(claude_root, codex_root):
    """Aggregate counts only; no transcript path or content leaves this call."""
    return {
        "claude": _side_census(claude_root, claude_user_blocks),
        "codex": _side_census(codex_root, codex_user_blocks),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-root", required=True)
    parser.add_argument("--codex-root", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(census(args.claude_root, args.codex_root),
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
