"""Fail unless the pushed tag matches package.json's version.

Usage: python3 scripts/check-release-version.py v1.2.3
"""

import json
import sys
from pathlib import Path

package = json.loads((Path(__file__).resolve().parent.parent / "package.json").read_text())
tag = (sys.argv[1] if len(sys.argv) > 1 else "").removeprefix("refs/tags/").removeprefix("v")
if tag != package["version"]:
    sys.exit(
        f"tag {sys.argv[1:] or '(missing)'} does not match {package['name']} {package['version']}"
    )
print(f"releasing {package['name']} {package['version']}")
