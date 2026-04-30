"""
Branding — operator-supplied firm name + short-name for email footers.

The Keystone product name stays hardcoded ('Keystone' is the app
itself, not the operating firm). What varies per-firm is:

  firm_name        full legal name printed in footers
                   (e.g. 'GoldStandard Wealth Pvt Ltd')
  firm_short_name  short brand mark used as a banner accent
                   (e.g. 'GoldStandard')

Sourced from ``config/azure.json:branding`` so the operator can edit
once at deploy time and have it apply everywhere. Falls back to the
GoldStandard defaults when no branding block exists, so existing
deployments keep their current visuals without a config change.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

# Hardcoded fallback — keeps the product visually intact for the
# existing tenant (GoldStandard) when no override is configured.
_DEFAULT_FIRM_NAME       = 'GoldStandard Wealth Pvt Ltd'
_DEFAULT_FIRM_SHORT_NAME = 'GoldStandard'


def _config_path() -> Path:
    cfg_dir = os.environ.get('KEYSTONE_CONFIG_DIR')
    if cfg_dir:
        return Path(cfg_dir) / 'azure.json'
    return Path(__file__).parent.parent / 'config' / 'azure.json'


def get() -> Dict[str, str]:
    """Return ``{firm_name, firm_short_name}``. Reads on every call —
    azure.json is small and writes happen at most once per deploy,
    so caching wouldn't buy anything meaningful."""
    p = _config_path()
    firm_name = _DEFAULT_FIRM_NAME
    firm_short = _DEFAULT_FIRM_SHORT_NAME
    if p.exists():
        try:
            cfg = json.loads(p.read_text(encoding='utf-8'))
            br = cfg.get('branding') or {}
            if isinstance(br, dict):
                if br.get('firm_name'):
                    firm_name = str(br['firm_name']).strip()
                if br.get('firm_short_name'):
                    firm_short = str(br['firm_short_name']).strip()
        except Exception as e:
            logger.warning(f'branding read failed, using defaults: {e}')
    return {'firm_name': firm_name, 'firm_short_name': firm_short}
