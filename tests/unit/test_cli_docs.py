from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_cli_reference_matches_the_commands() -> None:
    generated = subprocess.run([sys.executable, str(ROOT / "scripts/gen_cli_docs.py")], check=True,
                               capture_output=True, text=True, cwd=ROOT, timeout=60).stdout
    assert (ROOT / "docs/cli.md").read_text() == generated, (
        "docs/cli.md is stale: run `python scripts/gen_cli_docs.py > docs/cli.md`")
