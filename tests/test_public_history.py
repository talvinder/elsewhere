import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/scan_public_history.py"
SPEC = importlib.util.spec_from_file_location("scan_public_history", SCRIPT)
assert SPEC and SPEC.loader
history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history)


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def commit(root: Path, message: str) -> None:
    git(root, "add", ".")
    git(root, "commit", "-m", message)


class PublicHistoryTests(unittest.TestCase):
    def make_repo(self, root: Path) -> None:
        git(root, "init", "-b", "main")
        git(root, "config", "user.name", "History Test")
        git(root, "config", "user.email", "history@example.com")

    def test_scans_deleted_files_in_prior_snapshots_without_printing_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repo(root)
            secret = "AKIA" + "ABCDEFGHIJKLMNOP"
            (root / ".env").write_text(f"AWS_ACCESS_KEY_ID={secret}\n")
            commit(root, "add accidental credential")
            (root / ".env").unlink()
            (root / "README.md").write_text("public\n")
            commit(root, "remove accidental credential")
            report = history.scan_history(root)
            serialized = json.dumps(report)

        self.assertFalse(report["clean"])
        self.assertEqual(report["commits_scanned"], 2)
        self.assertIn("sensitive filename", serialized)
        self.assertIn("AWS access key", serialized)
        self.assertNotIn(secret, serialized)

    def test_rejects_internal_refs_and_deleted_internal_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repo(root)
            (root / "docs").mkdir()
            (root / "docs/STRATEGY.md").write_text("private plans\n")
            commit(root, "add planning document")
            git(root, "branch", "docs/distribution-strategy")
            (root / "docs/STRATEGY.md").unlink()
            (root / "README.md").write_text("public\n")
            commit(root, "remove planning document")
            report = history.scan_history(root)

        categories = {item["category"] for item in report["findings"]}
        self.assertIn("internal filename", categories)
        self.assertIn("internal ref name", categories)

    def test_allows_documented_synthetic_signed_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repo(root)
            (root / "test.py").write_text(
                'url = "https://storage.example/result?X-Amz-Signature=supersecret"\n'
            )
            commit(root, "add synthetic fixture")
            report = history.scan_history(root)

        self.assertTrue(report["clean"], report)

    def test_detects_nonstandard_credential_assignments_but_allows_placeholders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repo(root)
            credential = "actual-looking-value-123456"
            (root / "config.txt").write_text(
                f"AZURE_STORAGE_KEY={credential}\nAWS_SECRET_ACCESS_KEY=supersecret\n"
            )
            commit(root, "add config values")
            report = history.scan_history(root)
            serialized = json.dumps(report)

        self.assertIn("credential assignment", serialized)
        self.assertNotIn(credential, serialized)

    def test_private_known_values_are_matched_but_never_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repo(root)
            private_value = "personal-runner-name"
            (root / "config.json").write_text(f'{{"app":"{private_value}"}}\n')
            commit(root, "add config")
            deny = root / "deny.txt"
            deny.write_text(f"fly-app={private_value}\n")
            report = history.scan_history(root, deny)
            serialized = json.dumps(report)

        self.assertFalse(report["clean"])
        self.assertIn("known private value (fly-app)", serialized)
        self.assertNotIn(private_value, serialized)

    def test_private_provider_config_values_are_matched_without_being_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repo(root)
            private_value = "private-artifact-bucket"
            (root / "README.md").write_text(f"bucket: {private_value}\n")
            commit(root, "add accidental provider identity")
            config = root / "private-config.json"
            config.write_text(json.dumps({
                "artifact_store": {"provider": "tigris", "bucket": private_value}
            }))
            report = history.scan_history(root, provider_config_path=config)
            serialized = json.dumps(report)

        self.assertFalse(report["clean"])
        self.assertIn("known private value (artifact-bucket)", serialized)
        self.assertNotIn(private_value, serialized)

    def test_private_credentials_env_is_matched_without_being_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_repo(root)
            private_value = "private-secret-material-123456"
            (root / "README.md").write_text(f"old value: {private_value}\n")
            commit(root, "add accidental credential")
            credentials = root / "private.env"
            credentials.write_text(
                f"AWS_SECRET_ACCESS_KEY={private_value}\nAWS_REGION=auto\n"
            )
            report = history.scan_history(root, credentials_env_path=credentials)
            serialized = json.dumps(report)

        self.assertFalse(report["clean"])
        self.assertIn("known private value (aws-secret-access-key)", serialized)
        self.assertNotIn(private_value, serialized)


if __name__ == "__main__":
    unittest.main()
