# SPDX-License-Identifier: AGPL-3.0-only
"""Machine-readable contract between install.sh and main.py provider ops.

Both sides import/parse the same markers from this module so the 503
retry path no longer depends on human-readable installer prose:

* ``MARKER_HELPER_MISSING``: install.sh prints ``PROVIDER_CONTRACT=`` on
  stdout when ``--providers-only`` runs without a usable helper; main.py
  maps that exact key=value pair to HTTP 503. The string must stay
  ``KEY=VALUE``-shaped and is pinned on both sides by
  scripts/test_provider_admin.py.
* ``DETAILS_KEY_HELPER_MISSING`` / ``DETAILS_VALUE_RERUN_INSTALL``: the
  machine part of the 503 detail; the operator-facing hint text stays
  free-form.

Docstrings/comments stay prose on purpose; only these literals form the
contract.
"""

MARKER_PREFIX = "PROVIDER_CONTRACT="
MARKER_HELPER_MISSING = "PROVIDER_CONTRACT=helper-missing"
DETAILS_KEY_HELPER_MISSING = "reason"
DETAILS_VALUE_HELPER_MISSING = "helper-missing"
DETAILS_KEY_RERUN_INSTALL = "rerun"
DETAILS_VALUE_RERUN_INSTALL = "rerun-full-install"
