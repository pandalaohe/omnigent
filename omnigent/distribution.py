"""Distribution stamp for builds published outside the source repository.

The snapshot builder rewrites the ``DISTRIBUTION_VERSION`` line in a released
tree, e.g. to ``"0.21.0-wukong.12"``; source installs keep ``None`` and show
only the package version.
"""

from __future__ import annotations

DISTRIBUTION_NAME = "omnigent-wukong"
DISTRIBUTION_VERSION: str | None = None
