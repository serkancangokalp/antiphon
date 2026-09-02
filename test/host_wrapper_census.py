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

# The shapes that are not tags, mirrored from production (a contract test
# compares them): the AGENTS.md injection is a user prefix plus a fence, the
# external-agent relays are assistant prefixes, a compact summary is a
# host-set flag, and the interruption markers are exact literals.
AGENTS_INJECTION_HEAD = "# AGENTS.md instructions for "
EXTERNAL_AGENT_CALL = re.compile(
    r"\[external_agent_tool_call: ([A-Za-z0-9][A-Za-z0-9_.-]*)\](?:\n|$)")
EXTERNAL_AGENT_RESULT_HEAD = "[external_agent_tool_result]"
CLAUDE_HOST_LITERALS = ("[Request interrupted by user]",
                        "[Request interrupted by user for tool use]")


def opening_tag(text):
    """Return a complete opening tag name at the start of text, if present."""
    match = OPENING_TAG.match(text or "")
    return match.group(1) if match else None


def _prompt_source(record):
    source = record.get("promptSource", ABSENT)
    return source if isinstance(source, str) else "<non-string>"


def _join_text_blocks(blocks):
    """Production's user-message join: preserve whitespace, skip empty blocks."""
    return "\n\n".join(block for block in blocks
                         if isinstance(block, str) and block != "")


def claude_user_blocks(record):
    """One production-eligible Claude user message and its provenance."""
    if (not isinstance(record, dict) or record.get("type") != "user"
            or record.get("isMeta")):
        return []
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    source = _prompt_source(record)
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = _join_text_blocks(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text")
    else:
        text = ""
    return [(text, source)] if text != "" else []


def codex_user_blocks(record):
    """One joined Codex user message; Codex exposes no prompt-source field."""
    if not isinstance(record, dict) or record.get("type") != "response_item":
        return []
    payload = record.get("payload")
    if (not isinstance(payload, dict) or payload.get("type") != "message"
            or payload.get("role") != "user"):
        return []
    content = payload.get("content")
    if isinstance(content, list):
        text = _join_text_blocks(
            (block.get("text") or block.get("input_text") or "")
            for block in content if isinstance(block, dict))
    else:
        text = ""
    return [(text, ABSENT)] if text != "" else []


def claude_shapes(record):
    """Shape names for one Claude record: the host-set summary flag and the
    exact interruption literals, on production-eligible user messages."""
    blocks = claude_user_blocks(record)
    if not blocks:
        return []
    text = blocks[0][0]
    shapes = []
    if record.get("isCompactSummary"):
        shapes.append("compact_summary")
    if text in CLAUDE_HOST_LITERALS:
        shapes.append("interruption_literal")
    return shapes


def codex_assistant_blocks(record):
    """One joined Codex assistant message, the way the page reader joins it."""
    if not isinstance(record, dict) or record.get("type") != "response_item":
        return []
    payload = record.get("payload")
    if (not isinstance(payload, dict) or payload.get("type") != "message"
            or payload.get("role") != "assistant"):
        return []
    content = payload.get("content")
    if not isinstance(content, list):
        return []
    text = _join_text_blocks(
        (block.get("text") or block.get("input_text") or "")
        for block in content if isinstance(block, dict))
    return [text] if text != "" else []


def codex_shapes(record):
    """Shape names for one Codex record: the AGENTS.md injection on a user
    message, the external-agent relays on an assistant message."""
    shapes = []
    for text, _source in codex_user_blocks(record):
        if text.startswith(AGENTS_INJECTION_HEAD) and "\n<INSTRUCTIONS>" in text:
            shapes.append("agents_md_block")
    for text in codex_assistant_blocks(record):
        if EXTERNAL_AGENT_CALL.match(text):
            shapes.append("external_agent_call")
        elif text == EXTERNAL_AGENT_RESULT_HEAD or text.startswith(
                EXTERNAL_AGENT_RESULT_HEAD + "\n"):
            shapes.append("external_agent_result")
    return shapes


def _side_census(root, blocks_for, shapes_for):
    files = sorted(glob.glob(os.path.join(root, "**", "*.jsonl"),
                             recursive=True))
    malformed = 0
    messages = 0
    prompt_sources = collections.Counter()
    tags = collections.defaultdict(collections.Counter)
    shapes = collections.Counter()
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
                    messages += 1
                    prompt_sources[prompt_source] += 1
                    tag = opening_tag(text)
                    if tag is not None:
                        tags[tag][prompt_source] += 1
                for shape in shapes_for(record):
                    shapes[shape] += 1
    return {
        "files": len(files),
        "malformed_lines": malformed,
        "user_messages": messages,
        "prompt_sources": dict(sorted(prompt_sources.items())),
        "tags": {tag: dict(sorted(sources.items()))
                 for tag, sources in sorted(tags.items())},
        "shapes": dict(sorted(shapes.items())),
    }


def census(claude_root, codex_root):
    """Aggregate counts only; no transcript path or content leaves this call."""
    return {
        "claude": _side_census(claude_root, claude_user_blocks, claude_shapes),
        "codex": _side_census(codex_root, codex_user_blocks, codex_shapes),
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
