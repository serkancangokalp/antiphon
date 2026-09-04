#!/usr/bin/env python3
"""Verify the packed Antiphon bytes from an install-only CI job.

The caller must not check the repository out in this job.  The only inputs are
the artifact produced for the workflow SHA, its checksum, and an empty npm
prefix outside GITHUB_WORKSPACE.
"""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile


EXPECTED_FILES = frozenset({
    "BACKLOG.md",
    "LICENSE",
    "README.md",
    "bin/antiphon",
    "lib/antiphon.py",
    "lib/channel.mjs",
    "lib/identity.mjs",
    "lib/ledger.py",
    "lib/peers.py",
    "lib/workers.py",
    "package.json",
})
MAX_FILES = 64
MAX_MEMBERS = 128
MAX_TARBALL_BYTES = 4 * 1024 * 1024
MAX_UNPACKED_BYTES = 8 * 1024 * 1024
MAX_TAR_STREAM_BYTES = 8 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SETUP_FILES = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".codex/hooks.json",
    ".codex/config.toml",
    ".mcp.json",
    "CLAUDE.md",
    "AGENTS.md",
    ".antiphon/.gitignore",
)


class SmokeError(RuntimeError):
    pass


def _sha256_path(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _within(path, parent):
    try:
        return os.path.commonpath((str(path), str(parent))) == str(parent)
    except ValueError:
        return False


def validate_isolation(workspace, tarball, prefix):
    workspace = Path(workspace).absolute()
    resolved_workspace = workspace.resolve()
    tarball = Path(tarball).resolve()
    prefix = Path(prefix).resolve()
    if workspace.exists() or workspace.is_symlink():
        try:
            workspace_mode = workspace.lstat().st_mode
        except OSError as error:
            raise SmokeError("cannot stat the smoke workspace: %s" % error) from error
        if not stat.S_ISDIR(workspace_mode) or workspace.is_symlink():
            raise SmokeError("the smoke workspace is not a plain directory")
        if next(workspace.iterdir(), None) is not None:
            raise SmokeError("the smoke workspace is not empty; a checkout may be present")
    for label, path in (("tarball", tarball), ("npm prefix", prefix)):
        if _within(path, resolved_workspace):
            raise SmokeError("%s is inside the workspace" % label)
    if tarball == prefix or _within(tarball, prefix):
        raise SmokeError("the tarball must be outside the disposable npm prefix")


def _safe_relative(member):
    raw = member.name.rstrip("/") if member.isdir() else member.name
    if "\\" in raw or not raw.startswith("package/"):
        raise SmokeError("unsafe tar member: %r" % member.name)
    parts = raw.split("/")
    if (len(parts) < 2 or any(part in ("", ".", "..") for part in parts)
            or PurePosixPath(raw).is_absolute()):
        raise SmokeError("unsafe tar member: %r" % member.name)
    return "/".join(parts[1:])


def inspect_tarball(tarball, expected_sha256):
    tarball = Path(tarball)
    if not SHA256.fullmatch(str(expected_sha256)):
        raise SmokeError("the expected SHA-256 is not 64 lowercase hex characters")
    try:
        mode = tarball.lstat().st_mode
    except OSError as error:
        raise SmokeError("cannot stat tarball: %s" % error) from error
    if not stat.S_ISREG(mode) or tarball.is_symlink():
        raise SmokeError("the tarball is not a regular non-symlink file")
    if tarball.stat().st_size > MAX_TARBALL_BYTES:
        raise SmokeError("the tarball exceeds the compressed-size bound")
    actual = _sha256_path(tarball)
    if actual != expected_sha256:
        raise SmokeError("tarball SHA-256 mismatch: expected %s, got %s"
                         % (expected_sha256, actual))

    decompressed = 0
    try:
        with gzip.open(tarball, "rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                decompressed += len(block)
                if decompressed > MAX_TAR_STREAM_BYTES:
                    raise SmokeError("the tarball exceeds the decompressed-size bound")
    except SmokeError:
        raise
    except (gzip.BadGzipFile, EOFError, OSError) as error:
        raise SmokeError("cannot decompress tarball: %s" % error) from error

    files = {}
    directories = set()
    entries = set()
    unpacked = 0
    try:
        with tarfile.open(tarball, "r:gz") as archive:
            member_count = 0
            for member in archive:
                member_count += 1
                if member_count > MAX_MEMBERS:
                    raise SmokeError("the tarball exceeds the member-count bound")
                relative = _safe_relative(member)
                if relative in entries:
                    raise SmokeError("duplicate tar member: %s" % relative)
                entries.add(relative)
                if member.isdir():
                    directories.add(relative)
                    continue
                if not member.isfile():
                    raise SmokeError("unsupported tar member type: %r" % member.name)
                if len(files) >= MAX_FILES:
                    raise SmokeError("the tarball exceeds the file-count bound")
                unpacked += member.size
                if unpacked > MAX_UNPACKED_BYTES:
                    raise SmokeError("the tarball exceeds the unpacked-size bound")
                source = archive.extractfile(member)
                if source is None:
                    raise SmokeError("cannot read tar member: %s" % relative)
                digest = hashlib.sha256()
                measured = 0
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    measured += len(block)
                    digest.update(block)
                if measured != member.size:
                    raise SmokeError("tar member size mismatch: %s" % relative)
                files[relative] = (member.size, digest.hexdigest())
    except (tarfile.TarError, OSError) as error:
        raise SmokeError("cannot read tarball: %s" % error) from error

    if directories:
        raise SmokeError("unexpected packaged directories: %r" % sorted(directories))
    actual_files = set(files)
    if actual_files != EXPECTED_FILES:
        missing = sorted(EXPECTED_FILES - actual_files)
        extra = sorted(actual_files - EXPECTED_FILES)
        raise SmokeError("unexpected packaged files: missing=%r extra=%r"
                         % (missing, extra))
    return files


def verify_installed_files(manifest, package_root):
    package_root = Path(package_root).absolute()
    try:
        root_mode = package_root.lstat().st_mode
    except OSError as error:
        raise SmokeError("cannot stat the installed package root: %s" % error) from error
    if not stat.S_ISDIR(root_mode) or package_root.is_symlink():
        raise SmokeError("the installed package root is not a plain directory")
    resolved_root = package_root.resolve()
    for relative, (expected_size, expected_digest) in sorted(manifest.items()):
        target = package_root.joinpath(*PurePosixPath(relative).parts)
        try:
            mode = target.lstat().st_mode
        except OSError as error:
            raise SmokeError("installed file is missing: %s (%s)"
                             % (relative, error)) from error
        if not stat.S_ISREG(mode) or target.is_symlink():
            raise SmokeError("installed file is not regular: %s" % relative)
        if not _within(target.resolve(), resolved_root):
            raise SmokeError("installed file resolves outside the package: %s" % relative)
        actual_size = target.stat().st_size
        actual_digest = _sha256_path(target)
        if actual_size != expected_size or actual_digest != expected_digest:
            raise SmokeError("installed byte mismatch: %s" % relative)


def _run(argv, *, cwd, env, input_text=None, timeout=60, expected_codes=(0,)):
    command = [str(value) for value in argv]
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as error:
        raise SmokeError("command failed to run: %r: %s" % (argv, error)) from error
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired as cleanup_error:
                raise SmokeError(
                    "timed-out command group did not stop: %r" % argv
                ) from cleanup_error
        detail = (stderr or stdout or "").strip()[:2000]
        raise SmokeError("command timed out: %r: %s" % (argv, detail)) from error
    done = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if done.returncode not in expected_codes:
        detail = (done.stderr or done.stdout).strip()[:2000]
        raise SmokeError("command exited %d: %r: %s"
                         % (done.returncode, argv, detail))
    return done


def _handshake(binary, command, project, env, expected_version):
    request = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "installed-smoke", "version": "0"},
        },
    }) + "\n"
    done = _run([binary, command], cwd=project, env=env,
                input_text=request, timeout=60)
    answers = []
    for line in done.stdout.splitlines():
        if not line.strip():
            continue
        try:
            answers.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise SmokeError("%s emitted non-JSON stdout: %r"
                             % (command, line[:300])) from error
    answer = next((value for value in answers if value.get("id") == 1), None)
    try:
        version = answer["result"]["serverInfo"]["version"]
    except (KeyError, TypeError) as error:
        raise SmokeError("%s returned no initialize result: %r"
                         % (command, answer)) from error
    if version != expected_version:
        raise SmokeError("%s handshake version mismatch: %r != %r"
                         % (command, version, expected_version))


def _read_expected_sha(path):
    try:
        words = Path(path).read_text(encoding="ascii").split()
    except OSError as error:
        raise SmokeError("cannot read checksum file: %s" % error) from error
    if not words or not SHA256.fullmatch(words[0]):
        raise SmokeError("checksum file does not begin with a lowercase SHA-256")
    return words[0]


def installed_smoke(tarball, checksum_file, prefix, workspace):
    tarball = Path(tarball).absolute()
    prefix = Path(prefix).absolute()
    validate_isolation(workspace, tarball, prefix)
    manifest = inspect_tarball(tarball, _read_expected_sha(checksum_file))

    if prefix.exists() or prefix.is_symlink():
        try:
            prefix_mode = prefix.lstat().st_mode
        except OSError as error:
            raise SmokeError("cannot stat the npm prefix: %s" % error) from error
        if not stat.S_ISDIR(prefix_mode) or prefix.is_symlink():
            raise SmokeError("the npm prefix is not a plain directory")
        if next(prefix.iterdir(), None) is not None:
            raise SmokeError("the npm prefix is not empty")
    prefix.mkdir(parents=True, exist_ok=True)
    npm = shutil.which("npm")
    if not npm:
        raise SmokeError("npm is not on PATH")
    clean_env = os.environ.copy()
    for key in ("ANTIPHON_CWD", "ANTIPHON_NAME", "NODE_PATH", "PYTHONPATH"):
        clean_env.pop(key, None)
    _run([
        npm, "install", "--global", "--prefix", prefix, "--ignore-scripts",
        "--audit=false", "--fund=false", "--loglevel=error", tarball,
    ], cwd=prefix, env=clean_env, timeout=300)

    package_root = prefix / "lib" / "node_modules" / "antiphon"
    verify_installed_files(manifest, package_root)
    binary = prefix / "bin" / "antiphon"
    if not binary.exists():
        raise SmokeError("the global antiphon executable is missing")
    if binary.resolve() != (package_root / "bin" / "antiphon").resolve():
        raise SmokeError("the global executable does not resolve into the installed package")

    try:
        package = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
        version = package["version"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SmokeError("installed package.json has no usable version") from error
    if package.get("name") != "antiphon" or not isinstance(version, str) or not version:
        raise SmokeError("installed package identity is invalid")

    with tempfile.TemporaryDirectory(prefix="antiphon-installed-smoke-") as temporary:
        temporary = Path(temporary).resolve()
        home = temporary / "home"
        project = temporary / "project"
        home.mkdir()
        project.mkdir()
        env = clean_env.copy()
        env.update({
            "ANTIPHON_CWD": str(project),
            "ANTIPHON_NAME": "ci-smoke",
            "HOME": str(home),
            "PATH": str(prefix / "bin") + os.pathsep + clean_env.get("PATH", ""),
            "PYTHONNOUSERSITE": "1",
        })

        reported = _run([binary, "--version"], cwd=project, env=env).stdout.strip()
        if reported != "antiphon " + version:
            raise SmokeError("installed CLI version mismatch: %r" % reported)

        helped = _run([binary, "--help"], cwd=project, env=env)
        if (helped.stderr or "Usage:\n  antiphon setup" not in helped.stdout
                or "antiphon launch <host>" not in helped.stdout):
            raise SmokeError("installed CLI help is incomplete or used stderr")

        # Parse refusals must happen before host lookup/exec.  Put visible
        # sentinels first on PATH so the smoke proves the installed dispatcher
        # cannot invoke either host on malformed launch syntax.
        host_bin = temporary / "host-bin"
        host_bin.mkdir()
        host_log = temporary / "host-invoked"
        for host in ("claude", "codex"):
            stub = host_bin / host
            stub.write_text(
                '#!/bin/sh\n: > "$ANTIPHON_SMOKE_HOST_LOG"\nexit 99\n',
                encoding="ascii")
            stub.chmod(0o755)
        parse_env = env.copy()
        parse_env["PATH"] = str(host_bin) + os.pathsep + env["PATH"]
        parse_env["ANTIPHON_SMOKE_HOST_LOG"] = str(host_log)
        launch_refusals = (
            (("launch", "gemini"), "host must be claude or codex"),
            (("launch", "codex", "--bogus"), "unknown option"),
            (("launch", "claude", "--", "--dangerously-load-development-channels"),
             "already supplies the Antiphon development channel"),
        )
        for arguments, fragment in launch_refusals:
            refused = _run([binary, *arguments], cwd=project, env=parse_env,
                           expected_codes=(2,))
            if refused.stdout or fragment not in refused.stderr:
                raise SmokeError("installed launch refusal mismatch for %r" %
                                 (arguments,))
        if host_log.exists():
            raise SmokeError("a launch parse refusal invoked a host executable")

        import_env = env.copy()
        import_env["PYTHONPATH"] = str(package_root / "lib")
        imported = _run([
            sys.executable, "-c",
            "import os, antiphon; print(os.path.realpath(antiphon.__file__))",
        ], cwd=project, env=import_env).stdout.strip()
        if Path(imported).resolve() != (package_root / "lib" / "antiphon.py").resolve():
            raise SmokeError("Python imported Antiphon outside the installed package")

        _run([binary, "setup"], cwd=project, env=env)
        missing = [relative for relative in SETUP_FILES
                   if not (project / relative).is_file()]
        if missing:
            raise SmokeError("installed setup omitted files: %r" % missing)
        workspace_text = str(Path(workspace).resolve())
        for relative in SETUP_FILES:
            try:
                generated = (project / relative).read_text(encoding="utf-8")
            except UnicodeDecodeError as error:
                raise SmokeError("setup wrote non-UTF-8 text: %s" % relative) from error
            if workspace_text in generated:
                raise SmokeError("setup leaked the checkout workspace into %s" % relative)

        _handshake(binary, "mcp", project, env, version)
        _handshake(binary, "channel", project, env, version)

    print("installed tarball smoke: ok")
    print("version: %s" % version)
    print("first-party files verified: %d" % len(manifest))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--tarball", required=True)
    parser.add_argument("--checksum-file", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args(argv)
    try:
        installed_smoke(args.tarball, args.checksum_file,
                        args.prefix, args.workspace)
    except SmokeError as error:
        print("installed tarball smoke: FAIL: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
