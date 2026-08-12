import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_capacity.provenance import runtime_code_sha256, runtime_provenance


class RuntimeProvenanceTests(unittest.TestCase):
    def test_fingerprint_is_deterministic_and_covers_nested_provider_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent_capacity"
            (root / "providers").mkdir(parents=True)
            (root / "cli.py").write_text("print('one')\n")
            provider = root / "providers/fly.py"
            provider.write_text("REGION = 'bom'\n")
            first = runtime_code_sha256(root)
            second = runtime_code_sha256(root)
            provider.write_text("REGION = 'sin'\n")
            third = runtime_code_sha256(root)

        self.assertEqual(first, second)
        self.assertNotEqual(first, third)

    def test_installed_package_without_source_git_still_has_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent_capacity"
            root.mkdir()
            (root / "cli.py").write_text("print('installed')\n")
            value = runtime_provenance(root)

        self.assertEqual(value["capture_method"], "python-package-v1")
        self.assertIsNone(value["revision"])
        self.assertIsNone(value["dirty"])
        self.assertEqual(len(value["code_sha256"]), 64)

    def test_source_checkout_binds_fingerprint_to_revision_and_dirty_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            module = root / "src/agent_capacity"
            module.mkdir(parents=True)
            runtime = module / "cli.py"
            runtime.write_text("print('clean')\n")
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                [
                    "git", "-c", "user.name=Runtime Test",
                    "-c", "user.email=runtime@example.invalid",
                    "commit", "-q", "-m", "initial",
                ],
                cwd=root,
                check=True,
            )
            clean = runtime_provenance(module)
            runtime.write_text("print('dirty')\n")
            dirty = runtime_provenance(module)

        self.assertEqual(clean["capture_method"], "source-git-v1")
        self.assertEqual(len(clean["revision"]), 40)
        self.assertFalse(clean["dirty"])
        self.assertTrue(dirty["dirty"])
        self.assertEqual(clean["revision"], dirty["revision"])
        self.assertNotEqual(clean["code_sha256"], dirty["code_sha256"])


if __name__ == "__main__":
    unittest.main()
