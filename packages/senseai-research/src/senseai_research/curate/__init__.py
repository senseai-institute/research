# SPDX-License-Identifier: Apache-2.0
"""senseai-library: draft, validate and score ``library/<clade>.yaml`` before human review.

A research assistant that reads the literature on one clade, pulls out every
measured number with its paper, page, conditions and a verbatim quote, writes
a draft entry that validates against ``library/schema/clade.schema.json``,
cross-checks papers against each other and against retraction feeds, and
hands a human biologist a review sheet. It never invents a number and never
writes a number without a DOI and a quote.

Every HTTP and model response is cached under ``$SENSEAI_DATA/curate-cache/``
so a draft is reproducible offline.

Moved here from ``senseai_research.curate`` (spec D2, phase 5); profiles keeps a re-export
shim that warns. The profiles checkout it reads and writes is ``SENSEAI_PROFILES_REPO``
(``--root`` on the command line), no longer the directory this code sits in.
"""

import os
from pathlib import Path

__version__ = "0.1.0"
CURATOR = f"senseai-curate {__version__}"


def profiles_root() -> Path:
    """The profiles checkout: ``$SENSEAI_PROFILES_REPO``, else ``~/senseai/profiles``."""
    env = os.environ.get("SENSEAI_PROFILES_REPO")
    return Path(env).expanduser() if env else Path.home() / "senseai" / "profiles"
