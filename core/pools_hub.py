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
    def __init__(self, pools: List[dict], brokers: List[dict] = None):
        self._pools = pools
        self._brokers = brokers or []

    # ── Loading ──────────────────────────────────────────────────────────── #

    @classmethod
    def load(cls, path: str = None, broker_path: str = None) -> 'PoolsHub':
        # pools_hub.json — always present, authoritative for pool records
        if path is None:
            path = Path(__file__).parent.parent / 'config' / 'pools_hub.json'
        with open(path) as f:
            data = json.load(f)
        pools = data.get('pools', [])

        # broker_map.json — now also the home of broker-side CN aliases.
        # Optional so existing callers still work on boxes mid-migration.
        if broker_path is None:
            broker_path = Path(__file__).parent.parent / 'config' / 'broker_map.json'
        brokers: List[dict] = []
        try:
            with open(broker_path) as f:
                brokers = json.load(f).get('brokers', []) or []
        except FileNotFoundError:
            logger.info(f"broker_map.json not found at {broker_path} — aliases unavailable")
        except Exception as e:
            logger.warning(f"Failed to load broker_map.json: {e}")

        return cls(pools, brokers)

    @property
    def brokers(self) -> List[dict]:
        return self._brokers

    def broker_pool_aliases(self) -> List[dict]:
        """Flat list of pool aliases pulled from broker_map.json.

        Each entry: {pool_id, alias_code, note, broker_dealer_code}. Rows
        with blank alias_code are skipped — those are placeholder rows
        the UI shows for pools the broker hasn't registered a code for.
        """
        out: List[dict] = []
        for b in self._brokers:
            dc = (b.get('dealer_code') or '').strip()
            for a in b.get('pool_aliases', []) or []:
                code = (a.get('alias_code') or '').strip()
                if not code:
                    continue
                out.append({
                    'pool_id': a.get('pool_id', ''),
                    'alias_code': code,
                    'note': a.get('note', ''),
                    'broker_dealer_code': dc,
                })
        return out

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

        # Broker-side pool aliases (e.g. GSWP012 → aristos_hdfc) emit one
        # synthesized entry per alias so TradeReconEngine can resolve alt
        # codes back to the canonical MAPIN.
        pool_by_id = {p.get('pool_id'): p for p in self._pools if p.get('pool_id')}
        for alias in self.broker_pool_aliases():
            parent = pool_by_id.get(alias['pool_id'])
            if not parent or not parent.get('mapin'):
                continue
            entries.append({
                'dealer_account':  parent.get('dealer_account', ''),
                'mapin':           alias['alias_code'],
                'pool_name':       parent.get('display_name', ''),
                'scheme_name':     parent.get('custodian_code', ''),
                'custodian':       parent.get('custodian_bank', ''),
                'canonical_mapin': parent['mapin'],
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
                # Sub-account linkage — drives coverage inheritance for pools
                # that share the parent's custody file (e.g. Aristos NRO).
                'parent_pool_id':        p.get('parent_pool_id', ''),
                'is_sub_account':        bool(p.get('is_sub_account')),
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
        """Resolve an alias code to its canonical pool MAPIN.

        Aliases now live under brokers.pool_aliases; this walks that
        list and maps alias_code → the referenced pool's mapin.
        Returns alias_mapin unchanged if no match.
        """
        target = (alias_mapin or '').upper()
        if not target:
            return alias_mapin
        pool_by_id = {p.get('pool_id'): p for p in self._pools if p.get('pool_id')}
        for alias in self.broker_pool_aliases():
            if alias['alias_code'].upper() == target:
                parent = pool_by_id.get(alias['pool_id'])
                if parent and parent.get('mapin'):
                    return parent['mapin']
        return alias_mapin
