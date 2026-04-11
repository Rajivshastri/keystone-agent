"""
PoolsHub — unified accessor for pools_hub.json.

Replaces:
  config/pool_map.json              → used by TradeReconEngine
  config/mappings.json              → used by ReconEngine (holdings)
  config/icici_bank_pool_map.json   → used by BankVsWSReconEngine
  config/kotak_bank_pool_map.json   → used by BankVsWSReconEngine
  config/hdfc_bank_pool_map.json    → used by BankVsWSReconEngine

Usage:
  hub = PoolsHub.load()          # from config/pools_hub.json
  hub.pool_map_dict()            # compatible with old pool_map.json format
  hub.mappings_dict()            # compatible with old mappings.json format
  hub.bank_pool_map('icici')     # compatible with old *_bank_pool_map.json
  hub.ws_scheme_names_index()    # {scheme_name_lower: mapin}
"""
from __future__ import annotations
import json, os, logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class PoolsHub:
    def __init__(self, pools: List[dict]):
        self._pools = pools

    # ── Loading ──────────────────────────────────────────────────────────── #

    @classmethod
    def load(cls, path: str = None) -> 'PoolsHub':
        if path is None:
            path = Path(__file__).parent.parent / 'config' / 'pools_hub.json'
        with open(path) as f:
            data = json.load(f)
        return cls(data.get('pools', []))

    @property
    def pools(self) -> List[dict]:
        return self._pools

    def active(self) -> List[dict]:
        return [p for p in self._pools if p.get('active', True)]

    # ── Compatibility shims ───────────────────────────────────────────────── #

    def pool_map_dict(self) -> dict:
        """Return a dict compatible with the old pool_map.json format.
        Used by TradeReconEngine."""
        entries = []
        for p in self._pools:
            if not p.get('mapin'):
                continue
            entry = {
                'dealer_account':  p.get('dealer_account', ''),
                'mapin':           p['mapin'],
                'pool_name':       p.get('display_name', ''),
                'scheme_name':     p.get('custodian_code', ''),
                'custodian':       p.get('custodian_bank', ''),
                'ws_scheme_names': p.get('ws_scheme_names', []),
            }
            if p.get('canonical_mapin'):
                entry['canonical_mapin'] = p['canonical_mapin']
            entries.append(entry)
            # Generate alias entries (broker_cn_aliases)
            for alias in p.get('broker_cn_aliases', []):
                entries.append({
                    'dealer_account':  p.get('dealer_account', ''),
                    'mapin':           alias['mapin'],
                    'pool_name':       p.get('display_name', ''),
                    'scheme_name':     p.get('custodian_code', ''),
                    'custodian':       p.get('custodian_bank', ''),
                    'canonical_mapin': p['mapin'],
                })
        return {'pools': entries}

    def mappings_dict(self) -> dict:
        """Return a dict compatible with the old mappings.json format.
        Used by ReconEngine (holdings recon)."""
        strategy_mappings = []
        for p in self._pools:
            if not p.get('active', True):
                continue
            custodian = p.get('custodian_bank', '').lower()
            code      = p.get('custodian_code', '')
            if not custodian or not code:
                continue
            entry = {
                'id':                    p['pool_id'],
                'source':                custodian,
                'broker_code':           code,   # custodian code = "broker_code" in old schema
                'display_name':          p.get('display_name', ''),
                'default_ws_scheme_code': p.get('ws_scheme_code') or p.get('mapin', ''),
                'conditional_rules':     [
                    {k: v for k, v in r.items() if k in
                     ('if_client_id', 'if_client_id_prefix', 'then_ws_scheme_code')}
                    for r in p.get('ws_overrides', [])
                ],
            }
            strategy_mappings.append(entry)
        return {'strategy_mappings': strategy_mappings}

    def bank_pool_map(self, bank: str) -> dict:
        """Return {account_no: mapin_or_SKIP} for the given bank.
        Compatible with old *_bank_pool_map.json format.
        Inactive pools map to 'SKIP'."""
        result = {}
        bank_upper = bank.upper()
        for p in self._pools:
            acct = p.get('bank_account', '').strip()
            if not acct:
                continue
            b = p.get('bank', '').upper()
            if b != bank_upper:
                continue
            mapin = p.get('mapin', '')
            active = p.get('active', True)
            result[acct] = mapin if (active and mapin) else 'SKIP'
        return result

    def ws_scheme_names_index(self) -> Dict[str, str]:
        """Return {ws_scheme_name_lower: mapin} for all active pools."""
        idx = {}
        for p in self._pools:
            if not p.get('mapin') or not p.get('active', True):
                continue
            for name in p.get('ws_scheme_names', []):
                if name:
                    idx[name.lower().strip()] = p['mapin']
        return idx

    def ws_client_to_pool_map(self) -> dict:
        """
        Return a two-level mapping for WS client_code → pool MAPIN.

        Returns a dict with two keys:
          'exact':   {client_code_upper: mapin}   — checked first, wins always
          'prefix':  [(prefix_upper, mapin, exclude_set), ...]
                     — checked if no exact match; exclude_set lists codes
                       that belong to a more-specific pool (e.g. GWPJ0016
                       is excluded from the GWPJ* prefix so it falls through
                       to the exact match instead)

        pools_hub.json fields used:
          ws_client_codes           — list of exact WS client codes for this pool
          ws_client_prefix          — prefix string for bulk matching
          ws_client_prefix_exclude  — list of exact codes to skip in prefix matching
        """
        exact  = {}
        prefix = []

        for p in self._pools:
            mapin = p.get('mapin', '').strip()
            if not mapin:
                continue

            # Exact codes
            for code in p.get('ws_client_codes', []):
                exact[code.strip().upper()] = mapin

            # Prefix with optional exclusion set
            pfx = p.get('ws_client_prefix', '').strip().upper()
            if pfx:
                excl = {c.strip().upper() for c in p.get('ws_client_prefix_exclude', [])}
                prefix.append((pfx, mapin, excl))

        # Sort prefix entries longest-first for specificity
        prefix.sort(key=lambda x: -len(x[0]))
        return {'exact': exact, 'prefix': prefix}

    def pool_by_mapin(self, mapin: str) -> Optional[dict]:
        """Look up a pool by its primary MAPIN."""
        for p in self._pools:
            if p.get('mapin') == mapin:
                return p
        return None

    def pool_by_dealer_account(self, dealer_account: str) -> Optional[dict]:
        """Look up a pool by WS dealer grid account code."""
        da = dealer_account.upper()
        for p in self._pools:
            if p.get('dealer_account', '').upper() == da:
                return p
        return None

    def canonical_mapin(self, alias_mapin: str) -> str:
        """Resolve an alias MAPIN to its canonical MAPIN.
        Returns alias_mapin unchanged if no alias found."""
        for p in self._pools:
            for alias in p.get('broker_cn_aliases', []):
                if alias.get('mapin') == alias_mapin:
                    return p['mapin']   # canonical is the primary pool's mapin
        return alias_mapin
