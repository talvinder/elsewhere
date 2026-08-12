import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_capacity.artifact_transport import (
    _read_stable_source_file,
    package_source,
)


class SourcePackagingTests(unittest.TestCase):
    def test_source_bundle_is_private_and_collision_resistant(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / "app.py").write_text("print('private source')\n")

            first, _ = package_source(str(source), "same-job")
            second, _ = package_source(str(source), "same-job")
            try:
                self.assertNotEqual(first, second)
                self.assertEqual(first.stat().st_mode & 0o777, 0o600)
                self.assertEqual(second.stat().st_mode & 0o777, 0o600)
            finally:
                first.unlink(missing_ok=True)
                second.unlink(missing_ok=True)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires O_NOFOLLOW")
    def test_stable_read_refuses_a_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "secret.txt"
            secret.write_text("do not export\n")
            link = root / "source.txt"
            link.symlink_to(secret)

            content, snapshot, reason = _read_stable_source_file(link)

        self.assertIsNone(content)
        self.assertIsNone(snapshot)
        self.assertEqual(reason, "file changed during packaging")

    def test_failed_archive_creation_removes_private_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / "app.py").write_text("print('ok')\n")
            created = []
            real_named_temporary_file = tempfile.NamedTemporaryFile

            def record_temp(*args, **kwargs):
                value = real_named_temporary_file(*args, **kwargs)
                created.append(Path(value.name))
                return value

            with (
                mock.patch(
                    "agent_capacity.artifact_transport.tempfile.NamedTemporaryFile",
                    side_effect=record_temp,
                ),
                mock.patch(
                    "agent_capacity.artifact_transport.tarfile.open",
                    side_effect=RuntimeError("archive failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "archive failed"),
            ):
                package_source(str(source), "failed-job")

        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists())


if __name__ == "__main__":
    unittest.main()
