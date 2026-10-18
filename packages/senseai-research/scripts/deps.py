# SPDX-License-Identifier: Apache-2.0
"""Print this package's dependencies (plus extras), leaving some out. Used by CI.

python3 scripts/deps.py --without senseai-profiles --extra dev
"""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--without", action="append", default=[])
    ap.add_argument("--extra", action="append", default=[])
    a = ap.parse_args()
    proj = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]
    reqs = list(proj["dependencies"])
    for e in a.extra:
        reqs += proj["optional-dependencies"][e]
    keep = [r for r in reqs if re.split(r"[\[ <>=@;]", r, maxsplit=1)[0] not in a.without]
    print(" ".join(keep))


if __name__ == "__main__":
    main()
