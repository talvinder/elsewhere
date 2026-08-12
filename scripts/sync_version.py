#!/usr/bin/env python3
"""Synchronize release metadata from agent_capacity.version."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_capacity.version import __version__  # noqa: E402


def main(check: bool = False) -> int:
    version_path = ROOT / "VERSION"
    plugin_path = ROOT / "plugins/elsewhere/.codex-plugin/plugin.json"
    plugin = json.loads(plugin_path.read_text())
    expected_version = __version__ + "\n"
    changed = version_path.read_text() != expected_version or plugin.get("version") != __version__
    if check:
        if changed:
            print("release metadata is not synchronized", file=sys.stderr)
            return 1
        print(__version__)
        return 0
    version_path.write_text(expected_version)
    plugin["version"] = __version__
    plugin_path.write_text(json.dumps(plugin, indent=2) + "\n")
    print(__version__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main("--check" in sys.argv[1:]))
