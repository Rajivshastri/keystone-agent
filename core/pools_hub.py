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

Hierarchy (added for Pool Creation workflow — one IA → many Schemes → many Pools):
  hub.investment_approaches()    # [{ia_code, ia_name, ia_no}]
  hub.schemes()                  # [{scheme_name, ia_code, start_date, fund_manager, strategy}]
  hub.pools                      # existing; each pool gets new {ia_code, scheme_name} link keys
"""
from __future__ import annotations
import json, os, logging, tempfile
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class PoolsHub:
    def __init__(self, pools: List[dict],
                 brokers: List[dict] = None,
                 investment_approaches: List[dict] = None,
                 schemes: List[dict] = None,
                 source_path: Optional[Path] = None,
                 raw: Optional[dict] = None):
        self._pools = pools
        self._brokers = brokers or []
        self._ias = investment_approaches or []
        self._schemes = schemes or []
        # Remembered so save() can round-trip to the same file we loaded from,
        # and preserve any top-level metadata (_schema_version, _comment, …).
        self._source_path = source_path
        self._raw = raw or {}

    # ── Loading ──────────────────────────────────────────────────────────── #

    @classmethod
    def load(cls, path: str = None, broker_path: str = None) -> 'PoolsHub':
        if path is None:
            # Honor KEYSTONE_CONFIG_DIR so UI edits made on Azure (which land
            # in /home/keystone-config/pools_hub.json) are seen by loaders.
            # Falls back to the bundled config/ dir for local dev.
            _cfg_dir = os.environ.get('KEYSTONE_CONFIG_DIR')
            if _cfg_dir:
                path = Path(_cfg_dir) / 'pools_hub.json'
            else:
                path = Path(__file__).parent.parent / 'config' / 'pools_hub.json'
        with open(path) as f:
            data = json.load(f)
        pools = data.get('pools', [])
        ias     = data.get('investment_approaches', []) or []
        schemes = data.get('schemes', []) or []

        # broker_map.json — aliases now live under broker.pool_aliases.
        # Load it from the same directory as pools_hub.json (honors
        # KEYSTONE_CONFIG_DIR on Azure).
        if broker_path is None:
            broker_path = Path(path).parent / 'broker_map.json'
        brokers: List[dict] = []
        try:
            with open(broker_path) as f:
                brokers = json.load(f).get('brokers', []) or []
        except FileNotFoundError:
            logger.info(f"broker_map.json not found at {broker_path} — aliases unavailable")
        except Exception as e:
            logger.warning(f"Failed to load broker_map.json: {e}")

        return cls(pools, brokers,
                   investment_approaches=ias,
                   schemes=schemes,
                   source_path=Path(path),
                   raw=data)

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

        # Dealer-account aliases — same pool, alternative account string
        # in the dealer file. Emitted as extra entries so the engine's
        # dealer_account.upper() index resolves either spelling. Common
        # case: dealer file uses both a short code and a display name
        # for the same strategy (e.g. 'GSWP_57FORWARD' and
        # 'GoldStandard 57Forward Diversi').
        for p in self._pools:
            if not p.get('mapin'):
                continue
            for alias in (p.get('dealer_account_aliases') or []):
                alias = (alias or '').strip()
                if not alias:
                    continue
                entries.append({
                    'dealer_account':  alias,
                    'mapin':           p['mapin'],
                    'pool_name':       p.get('display_name', ''),
                    'scheme_name':     p.get('custodian_code', ''),
                    'custodian':       p.get('custodian_bank', ''),
                    'ws_scheme_names': p.get('ws_scheme_names', []),
                    'canonical_mapin': p['mapin'],
                })

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

    # ── Investment Approach / Scheme hierarchy (Pool Creation workflow) ──── #

    def investment_approaches(self) -> List[dict]:
        return self._ias

    def schemes(self) -> List[dict]:
        return self._schemes

    def ia_by_code(self, ia_code: str) -> Optional[dict]:
        if not ia_code:
            return None
        target = ia_code.strip().upper()
        for ia in self._ias:
            if (ia.get('ia_code') or '').strip().upper() == target:
                return ia
        return None

    def ia_by_name(self, ia_name: str) -> Optional[dict]:
        if not ia_name:
            return None
        target = ia_name.strip().lower()
        for ia in self._ias:
            if (ia.get('ia_name') or '').strip().lower() == target:
                return ia
        return None

    def schemes_for_ia(self, ia_code: str) -> List[dict]:
        target = (ia_code or '').strip().upper()
        return [s for s in self._schemes
                if (s.get('ia_code') or '').strip().upper() == target]

    def scheme_in_ia(self, ia_code: str, scheme_name: str) -> Optional[dict]:
        ia_target = (ia_code or '').strip().upper()
        nm_target = (scheme_name or '').strip().lower()
        for s in self._schemes:
            if ((s.get('ia_code') or '').strip().upper() == ia_target
                    and (s.get('scheme_name') or '').strip().lower() == nm_target):
                return s
        return None

    def pools_for_scheme(self, ia_code: str, scheme_name: str) -> List[dict]:
        ia_target = (ia_code or '').strip().upper()
        nm_target = (scheme_name or '').strip().lower()
        out = []
        for p in self._pools:
            if ((p.get('ia_code') or '').strip().upper() == ia_target
                    and (p.get('scheme_name') or '').strip().lower() == nm_target):
                out.append(p)
        return out

    def next_ia_seq(self) -> int:
        """Next sequence for a newly-created IA. Max existing + 1, else 1."""
        seqs = [int(ia.get('ia_no') or 0) for ia in self._ias]
        return (max(seqs) if seqs else 0) + 1

    # ── Upserts + atomic save ────────────────────────────────────────────── #

    def upsert_ia(self, ia: dict) -> dict:
        """Add or update an Investment Approach by ia_code. Returns the stored row."""
        code = (ia.get('ia_code') or '').strip().upper()
        if not code:
            raise ValueError("upsert_ia requires ia_code")
        for i, existing in enumerate(self._ias):
            if (existing.get('ia_code') or '').strip().upper() == code:
                merged = {**existing, **ia, 'ia_code': code}
                self._ias[i] = merged
                return merged
        row = {**ia, 'ia_code': code}
        self._ias.append(row)
        return row

    def upsert_scheme(self, scheme: dict) -> dict:
        """Add or update a Scheme by (ia_code, scheme_name). Returns the stored row."""
        ia_code = (scheme.get('ia_code') or '').strip().upper()
        nm      = (scheme.get('scheme_name') or '').strip()
        if not ia_code or not nm:
            raise ValueError("upsert_scheme requires ia_code and scheme_name")
        nm_key = nm.lower()
        for i, existing in enumerate(self._schemes):
            if ((existing.get('ia_code') or '').strip().upper() == ia_code
                    and (existing.get('scheme_name') or '').strip().lower() == nm_key):
                merged = {**existing, **scheme, 'ia_code': ia_code, 'scheme_name': nm}
                self._schemes[i] = merged
                return merged
        row = {**scheme, 'ia_code': ia_code, 'scheme_name': nm}
        self._schemes.append(row)
        return row

    def upsert_pool(self, pool: dict) -> dict:
        """Add or update a pool by pool_id. Returns the stored row."""
        pid = (pool.get('pool_id') or '').strip()
        if not pid:
            raise ValueError("upsert_pool requires pool_id")
        for i, existing in enumerate(self._pools):
            if (existing.get('pool_id') or '').strip() == pid:
                merged = {**existing, **pool, 'pool_id': pid}
                self._pools[i] = merged
                return merged
        row = {**pool, 'pool_id': pid}
        self._pools.append(row)
        return row

    def remove_pool(self, pool_id: str) -> bool:
        pid = (pool_id or '').strip()
        for i, p in enumerate(self._pools):
            if (p.get('pool_id') or '').strip() == pid:
                self._pools.pop(i)
                return True
        return False

    # ── Fund Manager emails ──────────────────────────────────────────────── #
    #
    # Each pool / scheme references a fund manager by name (the WS-side
    # picklist label). When the welcome email fires after authorize-ws
    # we need the FM's email for the Cc list, but the pool record only
    # carries the name. Store a flat ``{name: email}`` dict at the top
    # level of pools_hub.json under ``fund_manager_emails`` — operator
    # maintains it via Settings → Fund Managers.

    def fund_manager_email(self, name: str) -> str:
        """Look up a fund manager's email by name (case-insensitive).
        Returns '' when no email is configured — caller should treat
        as 'no Cc' rather than failing the send."""
        if not name:
            return ''
        emails = (self._raw.get('fund_manager_emails') or {})
        if not isinstance(emails, dict):
            return ''
        # Case-insensitive lookup so an operator typing 'sanjoy
        # bhattacharyya' matches an entry stored as 'Sanjoy Bhattacharyya'.
        target = name.strip().lower()
        for k, v in emails.items():
            if (k or '').strip().lower() == target:
                return (v or '').strip()
        return ''

    def fund_manager_emails(self) -> dict:
        """Return the full {name: email} dict for the Settings UI."""
        emails = (self._raw.get('fund_manager_emails') or {})
        if not isinstance(emails, dict):
            return {}
        return {(k or '').strip(): (v or '').strip()
                for k, v in emails.items() if (k or '').strip()}

    def set_fund_manager_emails(self, emails: dict) -> None:
        """Replace the fund-manager-emails dict. Saved on next ``save()``."""
        cleaned: dict = {}
        for k, v in (emails or {}).items():
            name  = (k or '').strip()
            email = (v or '').strip()
            if not name:
                continue
            cleaned[name] = email
        self._raw['fund_manager_emails'] = cleaned

    # ── Firm-wide bank accounts to exclude from recon ────────────────────── #
    #
    # Custodians like HDFC, Axis often hold a master / aggregator account in
    # the firm's name in addition to per-pool accounts. The aggregator
    # appears on the daily balance file but doesn't map to any pool, so its
    # closing balance has no WS counterpart and bank recon flags it as a
    # spurious break (or "NOT IN WS"). Operator marks those accounts here
    # and the bank-recon engine skips them — no recon row, no history
    # write, no opening-balance carry-over.
    #
    # Stored at the top level of pools_hub.json under
    # ``excluded_bank_accounts``: a list of {bank, account, note} dicts.
    # Account numbers alone are unique within and across banks, so
    # downstream callers may take just the account string set.

    def excluded_bank_accounts(self) -> List[str]:
        """Return account-number strings (no bank prefix) for every entry
        in the excluded list. Trims whitespace; drops blanks. Used by the
        bank-recon engine and bank_balance_history to filter out firm-wide
        accounts before they touch any reconciliation surface."""
        rows = self._raw.get('excluded_bank_accounts') or []
        out: List[str] = []
        for r in rows:
            if isinstance(r, dict):
                acct = (r.get('account') or '').strip()
            elif isinstance(r, str):
                acct = r.strip()
            else:
                continue
            if acct:
                out.append(acct)
        return out

    def excluded_bank_accounts_full(self) -> List[dict]:
        """Return the rich shape ``[{bank, account, note}, ...]`` for the
        operator UI. Defensive against legacy config files that stored
        bare strings — those get promoted to dicts with empty bank/note."""
        rows = self._raw.get('excluded_bank_accounts') or []
        out: List[dict] = []
        for r in rows:
            if isinstance(r, dict):
                out.append({
                    'bank':    (r.get('bank') or '').strip().upper(),
                    'account': (r.get('account') or '').strip(),
                    'note':    (r.get('note') or '').strip(),
                })
            elif isinstance(r, str) and r.strip():
                out.append({'bank': '', 'account': r.strip(), 'note': ''})
        return out

    def set_excluded_bank_accounts(self, rows: List[dict]) -> None:
        """Replace the excluded-accounts list. Persisted on the next
        ``save()``. Each row is normalised to ``{bank, account, note}``;
        blanks are dropped, duplicates collapsed (account-number is the
        dedupe key)."""
        seen = set()
        cleaned: List[dict] = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            acct = (r.get('account') or '').strip()
            if not acct or acct in seen:
                continue
            seen.add(acct)
            cleaned.append({
                'bank':    (r.get('bank') or '').strip().upper(),
                'account': acct,
                'note':    (r.get('note') or '').strip(),
            })
        self._raw['excluded_bank_accounts'] = cleaned

    def save(self, path: Optional[str] = None) -> Path:
        """Atomically write the hub back to disk.

        Writes to a sibling tempfile then renames — prevents torn JSON if the
        process dies mid-write. Preserves any top-level _schema_version /
        _comment keys from the original file.
        """
        target = Path(path) if path else self._source_path
        if target is None:
            raise ValueError("No path known for this PoolsHub — pass path=")
        target.parent.mkdir(parents=True, exist_ok=True)

        data = dict(self._raw)  # shallow copy of original top-level keys
        data['investment_approaches'] = self._ias
        data['schemes'] = self._schemes
        data['pools'] = self._pools

        # atomic rename: write to a temp file in the same dir, then os.replace
        fd, tmp = tempfile.mkstemp(prefix='.pools_hub.', suffix='.json.tmp',
                                   dir=str(target.parent))
        try:
            # encoding='utf-8' is mandatory: pool notes / display names
            # routinely contain U+2014 (em-dash) and U+2192 (right-arrow)
            # which crash json.dump on Windows where the default text
            # encoding is cp1252.
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write('\n')
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        self._source_path = target
        self._raw = data
        return target
