"""Print one version's section of CHANGELOG.md, for the GitHub release body.

Usage: python3 scripts/changelog-section.py CHANGELOG.md 1.2.3
"""

import re
import sys
from pathlib import Path

path, version = sys.argv[1], sys.argv[2]
text = Path(path).read_text()
# Headings like "## [0.4.0] — 2026-11-12" or "## 0.4.0".
heads = list(re.finditer(r"^## .*$", text, re.MULTILINE))
for i, m in enumerate(heads):
    if re.search(rf"(^|[\s\[]){re.escape(version)}(\]|\s|$)", m.group(0)[3:]):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = re.sub(r"\n---\s*$", "", text[m.end() : end].strip()).strip()
        # GitHub rejects a release body over 125000 characters (HTTP 422).
        if len(body) > 125000:
            sys.exit(f"section {version} is {len(body)} chars; GitHub's limit is 125000")
        print(body)
        sys.exit(0)
sys.exit(f"no section for {version} in {path}")
