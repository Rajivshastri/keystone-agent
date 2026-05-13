"""
core/mailbox_routing.py — single source of truth for per-category
mailbox routing.

Operators configure four mailbox slots (recon / nav / prices / trade) in
the Azure settings. Each source in sources.json is classified into one
of those categories by name; this module resolves a source to its
mailbox at fetch time. Both the Flask app and the Keystone agent
import from here so the two backends behave identically.
"""
from __future__ import annotations

from typing import Iterable

# ── Category source-name classifiers ──────────────────────────────── #

NAV_SOURCES: set[str] = {'value_research'}
PRICES_SOURCES: set[str] = {'vidal'}
# Trade sources come from broker_map.json: dealer / nsdl / exchange
# entries plus dynamic broker contract notes named broker_cn_<code>.
TRADE_SOURCE_NAMES: set[str] = {'dealer', 'nsdl', 'exchange'}
TRADE_SOURCE_PREFIXES: tuple[str, ...] = ('broker_cn_',)


def mailbox_for_source(source: dict, az_cfg: dict) -> str:
    """Resolve the mailbox to query for a given source.

    Resolution priority:
      1. Settings-level category override (recon_mailbox / nav_mailbox /
         prices_mailbox / trade_mailbox) — when set, redirects an
         entire category. This is the operator's lever for moving a
         whole class of sources to a different inbox without editing
         per-source config.
      2. Source's own ``mailbox`` field from sources.json.
      3. Empty string — the email_ingestor interprets this as
         "use the default mailbox" (self.mailbox / operations@).
    """
    name = (source.get('name') or '').strip().lower()
    if name in NAV_SOURCES:
        override = (az_cfg.get('nav_mailbox') or '').strip()
    elif name in PRICES_SOURCES:
        override = (az_cfg.get('prices_mailbox') or '').strip()
    elif name in TRADE_SOURCE_NAMES or name.startswith(TRADE_SOURCE_PREFIXES):
        override = (az_cfg.get('trade_mailbox') or '').strip()
    else:
        override = (az_cfg.get('recon_mailbox') or '').strip()
    if override:
        return override
    return (source.get('mailbox') or '').strip()


def group_sources_by_mailbox(
    sources: Iterable[dict],
    az_cfg: dict,
) -> dict[str, list[dict]]:
    """Group an iterable of source dicts by the mailbox each resolves to.

    Returns a dict ``{mailbox_override_str: [source, ...]}`` where the
    key is what the caller should pass as ``mailbox_override`` to
    ``EmailIngestor.fetch_for_date`` / ``fetch_for_range``. An empty
    string key means "default mailbox" — the ingestor's own
    ``self.mailbox`` (operations@).
    """
    groups: dict[str, list[dict]] = {}
    for src in sources:
        mb = mailbox_for_source(src, az_cfg)
        groups.setdefault(mb, []).append(src)
    return groups
