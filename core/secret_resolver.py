"""
Secret Resolver
---------------
Two-layer secret lookup so production passwords never have to live in
config/sources.json (which is checked into the repo) or be edited
through the UI in plain text.

Resolution priority (first non-empty wins):

  1. Env var  KEYSTONE_<SOURCE>_<FIELD>
              e.g. KEYSTONE_ICICI_ZIP_PASSWORD,
                   KEYSTONE_AXIS_FILE_PASSWORD
  2. sources.json field value (fallback — fine for local dev)

When running on Azure (`WEBSITE_SITE_NAME` set) and falling back to
sources.json with a non-empty plain-text value, this module emits a
one-time WARNING per (source, field). That nudges operators to migrate
the secret to App Settings without breaking the deployment.

A blank fallback never warns — the source simply has no password.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_warned_fallback: set = set()


def _env_key(source_name: str, field: str) -> str:
    """KEYSTONE_<SOURCE>_<FIELD> with non-alphanumerics normalised to _"""
    def _norm(s: str) -> str:
        return ''.join(c if c.isalnum() else '_' for c in s).upper()
    return f"KEYSTONE_{_norm(source_name)}_{_norm(field)}"


def resolve_source_secret(source_name: str, field: str,
                          fallback: str = '') -> str:
    """Return the secret value for `(source_name, field)`.

    Env var wins; sources.json is the fallback. On Azure, falling back to
    a non-empty sources.json value emits a one-time WARNING per
    (source, field) — visible in App Service log stream so operators
    notice the dependency.
    """
    env_name = _env_key(source_name, field)
    env_val = os.environ.get(env_name, '')
    if env_val:
        return env_val

    fb = (fallback or '').strip()
    if fb and os.environ.get('WEBSITE_SITE_NAME'):
        key = (source_name, field)
        if key not in _warned_fallback:
            logger.warning(
                f"Secret fallback: {field} for source {source_name!r} is "
                f"being read from sources.json (plain text). To remove the "
                f"plain-text dependency on Azure, set the App Setting "
                f"{env_name} and clear the sources.json field."
            )
            _warned_fallback.add(key)
    return fb


def resolve_from_source_dict(source: dict, field: str) -> str:
    """Convenience wrapper — pulls source['name'] + source[field] for you."""
    if not source:
        return ''
    name = source.get('name') or source.get('source') or ''
    return resolve_source_secret(name, field, source.get(field, ''))


# ----- testing helpers ------------------------------------------------ #

def _reset_warned():
    """Clear the one-time-warning cache. Used by tests."""
    _warned_fallback.clear()
