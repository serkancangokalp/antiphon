#!/usr/bin/env python3
"""Count host-owned opening wrappers without retaining transcript content."""

import argparse
import collections
import errno
import json
import os
import re
import stat


OPENING_TAG = re.compile(r"^\s*<([A-Za-z][A-Za-z0-9_-]*)(?=[\s>/])")
ABSENT = "<absent>"
NON_STRING_SOURCE = "<non-string>"
OTHER_SOURCE = "<other>"
KNOWN_PROMPT_SOURCES = frozenset(("system", "sdk", "typed"))
MAX_TAG_LENGTH = 64
# Reserve one key each for an overlong tag and cardinality overflow.  The
# census is a diagnostic over untrusted transcript bytes; its JSON output and
# in-memory aggregation must stay bounded even if every line invents a tag.
MAX_TAG_KEYS = 256
OVERLONG_TAG = "<overlong-tag>"
ADDITIONAL_TAGS = "<additional-tags>"

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
    if not match:
        return None
    tag = match.group(1)
    return tag if len(tag) <= MAX_TAG_LENGTH else OVERLONG_TAG


def _prompt_source(record):
    if "promptSource" not in record:
        return ABSENT
    source = record.get("promptSource")
    if not isinstance(source, str):
        return NON_STRING_SOURCE
    return source if source in KNOWN_PROMPT_SOURCES else OTHER_SOURCE


def _count_tag(target, tag, source, amount=1):
    """Increment one tag without allowing unbounded key cardinality."""
    reserved = (OVERLONG_TAG, ADDITIONAL_TAGS)
    ordinary = sum(1 for key in target if key not in reserved)
    if (tag not in target and tag not in reserved
            and ordinary >= MAX_TAG_KEYS - len(reserved)):
        tag = ADDITIONAL_TAGS
    target[tag][source] += amount


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
        if _is_codex_host_block(text):
            shapes.append("agents_md_block")
    for text in codex_assistant_blocks(record):
        if EXTERNAL_AGENT_CALL.match(text):
            shapes.append("external_agent_call")
        elif text == EXTERNAL_AGENT_RESULT_HEAD or text.startswith(
                EXTERNAL_AGENT_RESULT_HEAD + "\n"):
            shapes.append("external_agent_result")
    return shapes


def _is_codex_host_block(text):
    """Production's complete ordered AGENTS.md wrapper predicate."""
    if not isinstance(text, str) or not text.startswith(AGENTS_INJECTION_HEAD):
        return False
    opening = text.find("\n<INSTRUCTIONS>")
    return opening >= 0 and "</INSTRUCTIONS>" in text[opening:]


def _filesystem_safe_relative(value):
    """Mirror production's string-to-filesystem admission boundary."""
    if (not isinstance(value, str) or not value or "\0" in value
            or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
        return False
    try:
        os.fsencode(value)
    except (UnicodeError, ValueError):
        return False
    return True


def _safe_inventory(root):
    """Return paths, unsafe entries, capability status, and root error.

    Production opens every transcript one component at a time with
    ``O_NOFOLLOW``. The census uses the same admission boundary so content in
    a linked leaf or linked directory can never justify a production filter.
    It reports those paths only as aggregate refusal counts. A missing or
    unreadable root is different from an existing empty one and is carried as
    a bounded category, never a path or host error string.
    """
    files = []
    refused = []
    required_flags = all(hasattr(os, name) for name in
                         ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))
    supported = (required_flags
                 and os.open in getattr(os, "supports_dir_fd", set())
                 and os.stat in getattr(os, "supports_dir_fd", set())
                 and os.stat in getattr(os, "supports_follow_symlinks", set())
                 and os.listdir in getattr(os, "supports_fd", set()))
    if not supported:
        return files, refused, True, None
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        root_fd = os.open(os.path.realpath(root), os.O_RDONLY | os.O_DIRECTORY)
    except NotImplementedError:
        return files, refused, True, None
    except FileNotFoundError:
        return files, refused, False, "missing"
    except PermissionError:
        return files, refused, False, "unreadable"
    except OSError as error:
        reason = "not-directory" if error.errno == errno.ENOTDIR else "io-error"
        return files, refused, False, reason

    root_walk_error = None

    def walk(directory_fd, prefix=""):
        nonlocal root_walk_error
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as error:
            if not prefix:
                root_walk_error = ("unreadable" if error.errno in
                                   (errno.EACCES, errno.EPERM) else "io-error")
                return
            refused.append((prefix, True))
            return
        for name in names:
            relative = os.path.join(prefix, name) if prefix else name
            if not _filesystem_safe_relative(relative):
                # Production rejects this catalog string before it can learn
                # whether the entry is a leaf or a directory. Preserve that
                # uncertainty as one refusal and never inspect its content.
                refused.append((relative, True))
                continue
            try:
                info = os.stat(name, dir_fd=directory_fd,
                               follow_symlinks=False)
            except OSError:
                # The name may be a directory containing production leaves.
                # With no metadata, classifying it as a leaf would understate
                # the exact scope this census could not inspect.
                refused.append((relative, True))
                continue
            if stat.S_ISLNK(info.st_mode):
                # lstat cannot safely tell whether the target is a leaf or a
                # directory. Keep the ambiguity: a dotted link can still hide
                # recursive production candidates.
                refused.append((relative, True))
                continue
            if stat.S_ISDIR(info.st_mode):
                # Production enumerates candidate-shaped names before its
                # safe opener proves they are regular files.  Count this
                # directory as that refused leaf, then recurse as well: its
                # children may be independent production candidates.
                if name.endswith(".jsonl"):
                    refused.append((relative, False))
                try:
                    child = os.open(name, directory_flags, dir_fd=directory_fd)
                except OSError:
                    refused.append((relative, True))
                    continue
                try:
                    try:
                        child_info = os.fstat(child)
                    except OSError:
                        refused.append((relative, True))
                        continue
                    if stat.S_ISDIR(child_info.st_mode):
                        walk(child, relative)
                    else:
                        refused.append((relative, True))
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and name.endswith(".jsonl"):
                files.append(relative)
            elif name.endswith(".jsonl"):
                refused.append((relative, False))

    try:
        walk(root_fd)
    except NotImplementedError:
        # Capability sets are advisory. A runtime refusal makes the whole
        # census unsupported; never publish a prefix of a partial walk.
        return [], [], True, None
    finally:
        os.close(root_fd)
    if root_walk_error is not None:
        return [], [], False, root_walk_error
    # One filesystem entry is one refusal.  A candidate-shaped directory can
    # first be classified as a refused leaf and then fail while being opened
    # for recursion; merge those facts instead of double-counting the path.
    merged_refusals = {}
    for path, may_be_directory in refused:
        merged_refusals[path] = (
            merged_refusals.get(path, False) or may_be_directory)
    return sorted(files), sorted(merged_refusals.items()), False, None


def _open_safe(root, relative):
    """Open one inventory member through no-follow directory descriptors."""
    if not _filesystem_safe_relative(relative):
        return None
    parts = relative.split(os.sep)
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    if (not all(hasattr(os, name) for name in
                ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))
            or os.open not in getattr(os, "supports_dir_fd", set())):
        return None
    directories = []
    leaf = None
    try:
        current = os.open(os.path.realpath(root), os.O_RDONLY | os.O_DIRECTORY)
        directories.append(current)
        for component in parts[:-1]:
            current = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current)
            directories.append(current)
        leaf = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=current)
        if not stat.S_ISREG(os.fstat(leaf).st_mode):
            os.close(leaf)
            leaf = None
            return None
        source = os.fdopen(leaf, encoding="utf-8", errors="replace")
        leaf = None
        return source
    except (OSError, NotImplementedError):
        return None
    finally:
        if leaf is not None:
            os.close(leaf)
        for descriptor in reversed(directories):
            os.close(descriptor)


def _stats(root, files, blocks_for, shapes_for, inventory_refusals=0):
    """Aggregate one explicit path set without exposing any member."""
    malformed = 0
    messages = 0
    prompt_sources = collections.Counter()
    tags = collections.defaultdict(collections.Counter)
    shapes = collections.Counter()
    refused_files = inventory_refusals
    for relative in files:
        source = _open_safe(root, relative)
        if source is None:
            refused_files += 1
            continue
        local_malformed = 0
        local_messages = 0
        local_sources = collections.Counter()
        local_tags = collections.defaultdict(collections.Counter)
        local_shapes = collections.Counter()
        try:
            with source:
                for line in source:
                    try:
                        record = json.loads(line)
                    except Exception:  # malformed input includes RecursionError
                        local_malformed += 1
                        continue
                    # Some JSON decoders reject extreme container depth while
                    # newer ones accept it. A transcript line is a host record
                    # only when its top level is an object; count every other
                    # JSON value as malformed on every supported runtime.
                    if not isinstance(record, dict):
                        local_malformed += 1
                        continue
                    for text, prompt_source in blocks_for(record):
                        local_messages += 1
                        local_sources[prompt_source] += 1
                        tag = opening_tag(text)
                        if tag is not None:
                            _count_tag(local_tags, tag, prompt_source)
                    for shape in shapes_for(record):
                        local_shapes[shape] += 1
        except OSError:
            # A prefix of a file is not a trustworthy observation of that
            # file. Discard every local count and report one refusal.
            refused_files += 1
            continue
        malformed += local_malformed
        messages += local_messages
        prompt_sources.update(local_sources)
        shapes.update(local_shapes)
        for tag, sources in local_tags.items():
            for source, amount in sources.items():
                _count_tag(tags, tag, source, amount)
    return {
        "files": len(files),
        "refused_files": refused_files,
        "malformed_lines": malformed,
        "user_messages": messages,
        "prompt_sources": dict(sorted(prompt_sources.items())),
        "tags": {tag: dict(sorted(sources.items()))
                 for tag, sources in sorted(tags.items())},
        "shapes": dict(sorted(shapes.items())),
    }


def _side_census(root, kind, blocks_for, shapes_for):
    """Split the host inventory at production's exact candidate boundary."""
    all_files, refused, unsupported, root_error = _safe_inventory(root)

    def production_member(path):
        if kind == "claude":
            # `root` is ~/.claude/projects: production first selects one
            # immediate project directory, then only its immediate
            # transcripts. Nested subagent transcripts are not candidates.
            return len(path.split(os.sep)) == 2
        parts = path.split(os.sep)
        return (all(not part.startswith(".") for part in parts)
                and os.path.basename(path).startswith("rollout-"))

    def production_refusal(item):
        path, may_be_directory = item
        parts = path.split(os.sep)
        basename = parts[-1] if parts else ""
        if kind == "claude":
            return ((len(parts) == 2 and basename.endswith(".jsonl"))
                    or (len(parts) == 1 and may_be_directory))
        # Codex discovers matching leaves recursively. Any symlink may be a
        # directory containing such leaves, regardless of its own suffix.
        visible = all(not part.startswith(".") for part in parts)
        return visible and ((basename.startswith("rollout-")
                            and basename.endswith(".jsonl"))
                           or may_be_directory)

    admitted = [path for path in all_files if production_member(path)]
    admitted_set = set(admitted)
    excluded = [path for path in all_files if path not in admitted_set]
    refused_production = sum(1 for item in refused if production_refusal(item))
    refused_excluded = len(refused) - refused_production
    return {
        "all_files": len(all_files),
        "refused_paths": len(refused),
        "unsupported_platform": unsupported,
        "root_error": root_error,
        "production": _stats(
            root, admitted, blocks_for, shapes_for, refused_production),
        "excluded": _stats(
            root, excluded, blocks_for, shapes_for, refused_excluded),
    }


def census(claude_projects_root, codex_root):
    """Aggregate counts only; no transcript path or content leaves this call."""
    return {
        "claude": _side_census(
            claude_projects_root, "claude", claude_user_blocks, claude_shapes),
        "codex": _side_census(
            codex_root, "codex", codex_user_blocks, codex_shapes),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--claude-projects-root", "--claude-root", dest="claude_root",
        required=True,
        help="Claude's projects directory; the shorter old flag is kept as an alias")
    parser.add_argument("--codex-root", required=True,
                        help="Codex's recursive sessions directory")
    args = parser.parse_args(argv)
    result = census(args.claude_root, args.codex_root)
    print(json.dumps(result, indent=2, sort_keys=True))
    incomplete = any(
        side["unsupported_platform"] or side["root_error"] is not None
        for side in result.values())
    return 1 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
