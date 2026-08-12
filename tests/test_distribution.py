import importlib.util
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_distribution.py"
SPEC = importlib.util.spec_from_file_location("check_distribution", SCRIPT)
assert SPEC and SPEC.loader
check_distribution = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_distribution)


def add_file(archive, name, value=b"ok"):
    info = tarfile.TarInfo(name)
    info.size = len(value)
    archive.addfile(info, io.BytesIO(value))


class DistributionTests(unittest.TestCase):
    def test_sdist_rejects_an_unexpected_local_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "example.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                for path in check_distribution.SDIST_REQUIRED:
                    add_file(archive, f"elsewhere_run-1/{path}")
                add_file(archive, "elsewhere_run-1/.release-audit/bin/python")

            with self.assertRaisesRegex(ValueError, "unexpected roots.*release-audit"):
                check_distribution.check_sdist(archive_path)

    def test_sdist_rejects_links_even_under_an_allowed_root(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "example.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                for path in check_distribution.SDIST_REQUIRED:
                    add_file(archive, f"elsewhere_run-1/{path}")
                link = tarfile.TarInfo("elsewhere_run-1/src/python")
                link.type = tarfile.SYMTYPE
                link.linkname = "/usr/bin/python3"
                archive.addfile(link)

            with self.assertRaisesRegex(ValueError, "symbolic or hard link"):
                check_distribution.check_sdist(archive_path)


if __name__ == "__main__":
    unittest.main()
