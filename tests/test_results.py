import hashlib
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_capacity.results import (
    inspect_result_bundle,
    validate_result_paths,
    wrap_result_command,
)


def add_bytes(archive: tarfile.TarFile, name: str, value: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(value)
    archive.addfile(info, io.BytesIO(value))


class ResultContractTests(unittest.TestCase):
    def test_result_paths_must_remain_inside_workspace(self):
        self.assertEqual(validate_result_paths(["output/report.json", "output/report.json"]), ["output/report.json"])
        for invalid in ("", "/etc/passwd", "../secret", "output/../../secret", "bad\npath"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_result_paths([invalid])
        with self.assertRaises(ValueError):
            validate_result_paths(["x" * 513])

    def test_remote_wrapper_retains_output_exit_and_requested_files_before_upload(self):
        url = "https://example.blob.core.windows.net/results/job.tar.gz?sig=secret"
        command = wrap_result_command(
            "printf hello; mkdir -p output; printf data > output/result.txt; exit 17",
            "abc123", url, ["output/result.txt"], 300,
        )
        self.assertIn("stdout.txt", command)
        self.assertIn("stderr.txt", command)
        self.assertIn("exit_code", command)
        self.assertIn("checksums.sha256", command)
        self.assertIn("output/result.txt", command)
        self.assertIn("x-ms-blob-type: BlockBlob", command)
        self.assertIn("--retry 5", command)
        self.assertIn("COPYFILE_DISABLE=1 tar", command)
        self.assertIn(url, command)
        self.assertTrue(command.endswith('exit "$workload_exit"'))

    def test_bundle_verification_returns_exact_result_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "result.tar.gz"
            values = {
                "manifest.json": b'{"job_id":"job-1","requested_paths":["output/result.txt"],"version":1}\n',
                "exit_code": b"0\n",
                "stdout.txt": b"hello\n",
                "stderr.txt": b"",
                "missing.txt": b"",
                "files/output/result.txt": b"exact-result\n",
            }
            checksum_lines = "".join(
                f"{hashlib.sha256(value).hexdigest()}  ./{name}\n"
                for name, value in sorted(values.items())
            ).encode()
            with tarfile.open(bundle, "w:gz") as archive:
                for name, value in values.items():
                    add_bytes(archive, name, value)
                add_bytes(archive, "checksums.sha256", checksum_lines)
            inspected = inspect_result_bundle(bundle, root / "unpacked")
            self.assertEqual(inspected["job_id"], "job-1")
            self.assertEqual(inspected["exit_code"], 0)
            self.assertEqual(inspected["stdout"], "hello\n")
            self.assertEqual(inspected["returned_paths"], ["output/result.txt"])
            self.assertEqual(len(inspected["bundle_sha256"]), 64)
            self.assertGreater(inspected["bundle_size"], 0)

    def test_wrapper_round_trip_preserves_a_failing_workload_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            captured = root / "captured.tar.gz"
            curl = fake_bin / "curl"
            curl.write_text(
                "#!/bin/sh\n"
                "for arg in \"$@\"; do\n"
                "  case \"$arg\" in @*) cp \"${arg#@}\" \"$RESULT_CAPTURE\";; esac\n"
                "done\n"
            )
            curl.chmod(0o755)
            timeout = fake_bin / "timeout"
            timeout.write_text("#!/bin/sh\nshift 3\nexec \"$@\"\n")
            timeout.chmod(0o755)
            wrapped = wrap_result_command(
                "mkdir -p output; printf exact > output/result.txt; printf hello; printf warning >&2; exit 17",
                "job-roundtrip", "https://example.invalid/result?sig=secret",
                ["output/result.txt"], 60,
            )
            result = subprocess.run(
                ["/bin/sh", "-c", wrapped], cwd=root, text=True, capture_output=True,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{Path(shutil.which('sha256sum')).parent}:/usr/bin:/bin",
                    "RESULT_CAPTURE": str(captured),
                },
            )
            self.assertEqual(result.returncode, 17)
            inspected = inspect_result_bundle(captured, root / "roundtrip")
            self.assertEqual(inspected["exit_code"], 17)
            self.assertEqual(inspected["stdout"], "hello")
            self.assertEqual(inspected["stderr"], "warning")
            self.assertEqual((root / "roundtrip/files/output/result.txt").read_text(), "exact")

    def test_bundle_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "bad.tar.gz"
            with tarfile.open(bundle, "w:gz") as archive:
                add_bytes(archive, "../escape", b"bad")
            with self.assertRaises(ValueError):
                inspect_result_bundle(bundle, Path(directory) / "out")

    def test_bundle_rejects_expanded_size_over_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "large.tar.gz"
            with tarfile.open(bundle, "w:gz") as archive:
                add_bytes(archive, "large", b"123456")
            with patch("agent_capacity.results.MAX_RESULT_EXPANDED_BYTES", 5):
                with self.assertRaises(ValueError):
                    inspect_result_bundle(bundle, Path(directory) / "out")

    def test_bundle_rejects_checksum_paths_outside_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "checksum-traversal.tar.gz"
            values = {
                "manifest.json": b'{"job_id":"job","requested_paths":[],"version":1}',
                "exit_code": b"0\n", "stdout.txt": b"", "stderr.txt": b"",
                "missing.txt": b"",
                "checksums.sha256": b"0" * 64 + b"  ../../outside\n",
            }
            with tarfile.open(bundle, "w:gz") as archive:
                for name, value in values.items():
                    add_bytes(archive, name, value)
            with self.assertRaises(ValueError):
                inspect_result_bundle(bundle, root / "out")

    def test_bundle_rejects_unverified_extra_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "unverified-extra.tar.gz"
            values = {
                "manifest.json": b'{"job_id":"job","requested_paths":[],"version":1}',
                "exit_code": b"0\n", "stdout.txt": b"", "stderr.txt": b"",
                "missing.txt": b"",
            }
            checksums = "".join(
                f"{hashlib.sha256(value).hexdigest()}  ./{name}\n"
                for name, value in sorted(values.items())
            ).encode()
            with tarfile.open(bundle, "w:gz") as archive:
                for name, value in values.items():
                    add_bytes(archive, name, value)
                add_bytes(archive, "files/unverified.txt", b"not checksummed")
                add_bytes(archive, "checksums.sha256", checksums)
            with self.assertRaisesRegex(ValueError, "unverified files"):
                inspect_result_bundle(bundle, root / "out")

    def test_bundle_rejects_duplicate_archive_and_checksum_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            duplicate_archive = root / "duplicate-archive.tar.gz"
            with tarfile.open(duplicate_archive, "w:gz") as archive:
                add_bytes(archive, "stdout.txt", b"first")
                add_bytes(archive, "stdout.txt", b"second")
            with self.assertRaisesRegex(ValueError, "duplicate result archive path"):
                inspect_result_bundle(duplicate_archive, root / "archive-out")

            duplicate_checksum = root / "duplicate-checksum.tar.gz"
            values = {
                "manifest.json": b'{"job_id":"job","requested_paths":[],"version":1}',
                "exit_code": b"0\n", "stdout.txt": b"", "stderr.txt": b"",
                "missing.txt": b"",
            }
            lines = [
                f"{hashlib.sha256(value).hexdigest()}  ./{name}\n"
                for name, value in sorted(values.items())
            ]
            lines.append(lines[0])
            with tarfile.open(duplicate_checksum, "w:gz") as archive:
                for name, value in values.items():
                    add_bytes(archive, name, value)
                add_bytes(archive, "checksums.sha256", "".join(lines).encode())
            with self.assertRaisesRegex(ValueError, "duplicate result checksum path"):
                inspect_result_bundle(duplicate_checksum, root / "checksum-out")

    def test_bundle_rejects_malformed_manifest_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, manifest in (
                ("list", b"[]"),
                ("paths", b'{"job_id":"job","requested_paths":"output","version":1}'),
            ):
                bundle = root / f"malformed-{label}.tar.gz"
                values = {
                    "manifest.json": manifest,
                    "exit_code": b"0\n", "stdout.txt": b"", "stderr.txt": b"",
                    "missing.txt": b"",
                }
                checksums = "".join(
                    f"{hashlib.sha256(value).hexdigest()}  ./{name}\n"
                    for name, value in sorted(values.items())
                ).encode()
                with tarfile.open(bundle, "w:gz") as archive:
                    for name, value in values.items():
                        add_bytes(archive, name, value)
                    add_bytes(archive, "checksums.sha256", checksums)
                with self.assertRaises(ValueError):
                    inspect_result_bundle(bundle, root / f"malformed-{label}-out")


if __name__ == "__main__":
    unittest.main()
