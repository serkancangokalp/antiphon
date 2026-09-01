#!/usr/bin/env python3
"""Find the newest Claude transcript with one exact assistant text block."""

import json
from pathlib import Path
import sys


NO_MATCH = 3


def has_exact_assistant_text(path, expected):
    with path.open("rb") as source:
        for raw in source:
            if not raw.endswith(b"\n"):
                continue
            try:
                record = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict) or record.get("type") != "assistant":
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = block.get("text")
                if isinstance(text, str) and text.strip() == expected:
                    return True
    return False


def newest_match(root, expected):
    matches = []
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError("transcript root is not a directory")
    for path in root.iterdir():
        if not path.name.endswith(".jsonl") or not path.is_file():
            continue
        if not has_exact_assistant_text(path, expected):
            continue
        matches.append((path.stat().st_mtime_ns, path.name, path))
    return max(matches)[2] if matches else None


def main(argv):
    if len(argv) != 3 or not argv[2]:
        return 2
    try:
        found = newest_match(argv[1], argv[2])
    except OSError:
        print("marker probe: transcript root or file is not readable", file=sys.stderr)
        return 2
    if found is None:
        return NO_MATCH
    print(found)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
