"""Distribution stamp for builds published outside the source repository.

The snapshot builder rewrites the ``DISTRIBUTION_VERSION`` and
``DISTRIBUTION_RELEASE_REPO`` lines in a released tree, e.g. to
``"0.21.0-wukong.12"`` and ``"pandalaohe/omnigent-wukong"``; source installs
keep ``None`` and behave identically to upstream.
"""

from __future__ import annotations

DISTRIBUTION_NAME = "omnigent-wukong"
DISTRIBUTION_VERSION: str | None = None
DISTRIBUTION_RELEASE_REPO: str | None = None
