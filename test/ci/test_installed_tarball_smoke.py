import hashlib
import importlib.util
import io
import os
from pathlib import Path
import signal
import sys
import tarfile
import tempfile
import time
import unittest


HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "installed_tarball_smoke.py"
SPEC = importlib.util.spec_from_file_location("installed_tarball_smoke", MODULE_PATH)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


EXPECTED = {
    "BACKLOG.md",
    "CHANGELOG.md",
    "LICENSE",
    "README.md",
    "bin/antiphon",
    "docs/assets/antiphon-banner.svg",
    "lib/antiphon.py",
    "lib/channel.mjs",
    "lib/identity.mjs",
    "lib/ledger.py",
    "lib/peers.py",
    "lib/workers.py",
    "package.json",
}


class InstalledTarballSmokeTest(unittest.TestCase):
    def make_tarball(self, directory, additions=(), links=(), directories=()):
        path = Path(directory) / "antiphon.tgz"
        with tarfile.open(path, "w:gz") as archive:
            for relative in sorted(EXPECTED):
                payload = ("payload:" + relative).encode()
                info = tarfile.TarInfo("package/" + relative)
                info.size = len(payload)
                info.mode = 0o755 if relative == "bin/antiphon" else 0o644
                archive.addfile(info, io.BytesIO(payload))
            for name, payload in additions:
                raw = payload.encode()
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                archive.addfile(info, io.BytesIO(raw))
            for name, target in links:
                info = tarfile.TarInfo(name)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                archive.addfile(info)
            for name in directories:
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
        return path

    def digest(self, path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def install_fixture(self, root):
        root = Path(root)
        for relative in EXPECTED:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(("payload:" + relative).encode())

    def test_an_exact_safe_package_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            tarball = self.make_tarball(directory)
            manifest = smoke.inspect_tarball(tarball, self.digest(tarball))
            self.assertEqual(set(manifest), EXPECTED)

    def test_a_traversal_member_is_rejected_without_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            tarball = self.make_tarball(directory, additions=[("package/../escape", "x")])
            with self.assertRaisesRegex(smoke.SmokeError, "unsafe tar member"):
                smoke.inspect_tarball(tarball, self.digest(tarball))
            self.assertFalse((Path(directory) / "escape").exists())

    def test_a_link_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            tarball = self.make_tarball(
                directory, links=[("package/lib/alias.py", "../../outside")])
            with self.assertRaisesRegex(smoke.SmokeError, "unsupported tar member"):
                smoke.inspect_tarball(tarball, self.digest(tarball))

    def test_an_archive_cannot_hide_unbounded_directory_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "many-directories.tgz"
            with tarfile.open(path, "w:gz") as archive:
                for number in range(129):
                    info = tarfile.TarInfo("package/d%03d/" % number)
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
            with self.assertRaisesRegex(smoke.SmokeError, "member-count bound"):
                smoke.inspect_tarball(path, self.digest(path))

    def test_pax_metadata_counts_toward_the_decompressed_stream_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large-pax-metadata.tgz"
            with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
                for number, relative in enumerate(sorted(EXPECTED)):
                    payload = ("payload:" + relative).encode()
                    info = tarfile.TarInfo("package/" + relative)
                    info.size = len(payload)
                    if number == 0:
                        info.pax_headers = {
                            "comment": "x" * (smoke.MAX_UNPACKED_BYTES + 1),
                        }
                    archive.addfile(info, io.BytesIO(payload))
            self.assertLess(path.stat().st_size, 4 * 1024 * 1024)
            with self.assertRaisesRegex(smoke.SmokeError, "decompressed-size bound"):
                smoke.inspect_tarball(path, self.digest(path))

    def test_the_compressed_tarball_has_a_hashing_cost_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "too-large.tgz"
            with open(path, "wb") as stream:
                stream.truncate(4 * 1024 * 1024 + 1)
            with self.assertRaisesRegex(smoke.SmokeError, "compressed-size bound"):
                smoke.inspect_tarball(path, self.digest(path))

    def test_a_checksum_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            tarball = self.make_tarball(directory)
            with self.assertRaisesRegex(smoke.SmokeError, "SHA-256 mismatch"):
                smoke.inspect_tarball(tarball, "0" * 64)

    def test_an_extra_packaged_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            tarball = self.make_tarball(
                directory, additions=[("package/.antiphon/private.json", "secret")])
            with self.assertRaisesRegex(smoke.SmokeError, "unexpected packaged files"):
                smoke.inspect_tarball(tarball, self.digest(tarball))

    def test_an_extra_packaged_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            tarball = self.make_tarball(
                directory, directories=["package/unexpected-empty/"])
            with self.assertRaisesRegex(smoke.SmokeError, "unexpected packaged directories"):
                smoke.inspect_tarball(tarball, self.digest(tarball))

    def test_installed_bytes_must_equal_tarball_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            tarball = self.make_tarball(directory)
            manifest = smoke.inspect_tarball(tarball, self.digest(tarball))
            package_root = Path(directory) / "installed"
            self.install_fixture(package_root)
            (package_root / "lib/antiphon.py").write_text("mutated")
            with self.assertRaisesRegex(smoke.SmokeError, "installed byte mismatch"):
                smoke.verify_installed_files(manifest, package_root)

    def test_every_exact_installed_byte_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            tarball = self.make_tarball(directory)
            manifest = smoke.inspect_tarball(tarball, self.digest(tarball))
            package_root = Path(directory) / "installed"
            self.install_fixture(package_root)
            smoke.verify_installed_files(manifest, package_root)

    def test_a_symlink_cannot_masquerade_as_the_installed_package_root(self):
        with tempfile.TemporaryDirectory() as directory:
            tarball = self.make_tarball(directory)
            manifest = smoke.inspect_tarball(tarball, self.digest(tarball))
            real_root = Path(directory) / "real-installed"
            self.install_fixture(real_root)
            linked_root = Path(directory) / "linked-installed"
            linked_root.symlink_to(real_root, target_is_directory=True)
            with self.assertRaisesRegex(smoke.SmokeError, "package root is not a plain"):
                smoke.verify_installed_files(manifest, linked_root)

    def test_a_non_directory_prefix_is_a_controlled_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            tarball = self.make_tarball(directory)
            checksum = root / "checksum"
            checksum.write_text(self.digest(tarball) + "\n")
            prefix = root / "prefix"
            prefix.write_text("not a directory")
            try:
                smoke.installed_smoke(tarball, checksum, prefix, workspace)
            except Exception as error:
                self.assertIsInstance(error, smoke.SmokeError)
                self.assertRegex(str(error), "npm prefix is not a plain")
            else:
                self.fail("a file was accepted as an npm prefix")

    def test_installed_smoke_rejects_a_symlinked_tarball_before_npm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            real_tarball = self.make_tarball(directory)
            linked_tarball = root / "linked.tgz"
            linked_tarball.symlink_to(real_tarball)
            checksum = root / "checksum"
            checksum.write_text(self.digest(real_tarball) + "\n")
            prefix = root / "prefix"
            prefix.mkdir()
            (prefix / "sentinel").write_text("npm must not run")
            with self.assertRaisesRegex(smoke.SmokeError, "tarball is not a regular"):
                smoke.installed_smoke(linked_tarball, checksum, prefix, workspace)

    def test_the_smoke_job_workspace_must_be_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            (workspace / "checkout.txt").write_text("checkout")
            with self.assertRaisesRegex(smoke.SmokeError, "workspace is not empty"):
                smoke.validate_isolation(
                    workspace,
                    Path(directory) / "artifact/antiphon.tgz",
                    Path(directory) / "prefix",
                )

    def test_a_symlink_is_not_accepted_as_the_no_checkout_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_workspace = root / "real-workspace"
            real_workspace.mkdir()
            linked_workspace = root / "linked-workspace"
            linked_workspace.symlink_to(real_workspace, target_is_directory=True)
            with self.assertRaisesRegex(smoke.SmokeError, "workspace is not a plain"):
                smoke.validate_isolation(
                    linked_workspace,
                    root / "artifact/antiphon.tgz",
                    root / "prefix",
                )

    def test_artifact_and_prefix_must_be_outside_the_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            with self.assertRaisesRegex(smoke.SmokeError, "inside the workspace"):
                smoke.validate_isolation(
                    workspace,
                    workspace / "antiphon.tgz",
                    Path(directory) / "prefix",
                )

    def test_a_timed_out_wrapper_does_not_leave_its_child_running(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_pid_file = root / "child.pid"
            wrapper = root / "wrapper.py"
            wrapper.write_text(
                "import pathlib, subprocess, sys\n"
                "child = subprocess.Popen([\n"
                "    sys.executable, '-c',\n"
                "    'import time; time.sleep(60)',\n"
                "])\n"
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
                "child.wait()\n",
                encoding="utf-8",
            )
            child_pid = None
            try:
                with self.assertRaisesRegex(smoke.SmokeError, "timed out"):
                    smoke._run(
                        [sys.executable, wrapper, child_pid_file],
                        cwd=root,
                        env=os.environ.copy(),
                        timeout=0.5,
                    )
                child_pid = int(child_pid_file.read_text(encoding="ascii"))
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("the timed-out wrapper's child is still running")
            finally:
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
