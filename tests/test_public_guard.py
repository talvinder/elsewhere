import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts/check-no-internal.sh"


class PublicContentGuardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        scripts = self.root / "scripts"
        scripts.mkdir()
        shutil.copy2(GUARD, scripts / GUARD.name)
        subprocess.run(["git", "-C", str(self.root), "add", "scripts"], check=True)

    def tearDown(self):
        self.directory.cleanup()

    def run_guard(self, mode="--tracked"):
        return subprocess.run(
            ["sh", f"scripts/{GUARD.name}", mode],
            cwd=self.root,
            text=True,
            capture_output=True,
        )

    def track(self, relative: str, content: str = "public engineering content\n") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        subprocess.run(["git", "-C", str(self.root), "add", relative], check=True)
        return path

    def test_guard_does_not_block_its_own_marker_definition(self):
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_guard_blocks_internal_marker_content(self):
        marker = "ELSEWHERE:" + "INTERNAL"
        self.track("docs/architecture.md", f"{marker}\n")
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/architecture.md", result.stdout)

    def test_guard_blocks_strategy_filename_with_spaces(self):
        self.track("docs/LAUNCH PLAN.md")
        result = self.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/LAUNCH PLAN.md", result.stdout)

    def test_guard_ignores_tracked_files_removed_from_worktree(self):
        path = self.track("BRAND.md")
        path.unlink()
        result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
