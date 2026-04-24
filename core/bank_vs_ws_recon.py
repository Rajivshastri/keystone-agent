"""
Bank vs WS Reconciliation Engine
---------------------------------
Three-layer reconciliation per custodian pool account:

  Layer 1 — Direct balance match
    Σ WS closing balances (same MAPID, same bank_prefix) vs custodian pool closing
    Status: MATCH | SHORTFALL | EXCESS

  Layer 2 — Netting with artificial accounts (only when Layer 1 ≠ MATCH)
    Artificial accounts = custodian accounts at the same bank with NO Pool Master mapping.
    Net position = variance + Σ artificial account balances (same bank)
    Status: COVERED | NETTED BREAK | (if Layer1 = MATCH this layer is skipped)

  Layer 3 — Transaction matching
    For each custodian transaction:
      Find WS transactions on the same date where same MAPID and |Σ amounts| ≈ |cust amount|
      One custodian entry can match MANY WS entries (multi-client settlement)
    Status: MATCHED | CUST ONLY | WS ONLY | PARTIAL

Dataclasses
-----------
PoolReconResult  — one result per (MAPID, bank)
TxnMatchResult   — one per custodian transaction
BankReconSummary — top-level container
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TOLERANCE = 0.05   # INR — rounding across many client accounts can be > 0.01


# ── Transaction matching ──────────────────────────────────────────────────── #

@dataclass
class TxnMatchResult:
    date:            str
    cust_amount:     float        # signed (negative = debit from pool)
    cust_desc:       str
    cust_account:    str
    ws_amounts:      List[float]  # individual WS amounts matched
    ws_descs:        List[str]
    ws_accounts:     List[str]    # WS account keys (e.g. ICICI-002101019475)
    ws_sum:          float        # Σ ws_amounts
    variance:        float        # cust_amount - ws_sum
    status:          str          # MATCHED | CUST ONLY | WS ONLY | PARTIAL
    note:            str  = ''    # Human-readable explanation (e.g. un-booked sell)
    unbooked_sell:   bool = False # True when a custodian credit has no WS match

    @property
    def is_matched(self) -> bool:
        return self.status == 'MATCHED'

    def to_dict(self) -> dict:
        return {
            'date':          self.date,
            'cust_amount':   self.cust_amount,
            'cust_desc':     self.cust_desc,
            'cust_account':  self.cust_account,
            'ws_amounts':    self.ws_amounts,
            'ws_descs':      self.ws_descs,
            'ws_accounts':   self.ws_accounts,
            'ws_sum':        self.ws_sum,
            'variance':      self.variance,
            'status':        self.status,
            'note':          self.note,
            'unbooked_sell': self.unbooked_sell,
        }


# ── Per-pool result ───────────────────────────────────────────────────────── #

@dataclass
class PoolReconResult:
    mapid:               str
    strategy_name:       str
    bank:                str
    cust_account:        str

    # Opening balances (from previous working day's files)
    cust_opening:        float = 0.0
    ws_opening_sum:      float = 0.0
    has_opening_balance: bool  = True

    # Closing balances (from recon day's files)
    cust_closing:        float = 0.0
    ws_closing_sum:      float = 0.0

    # Transactions
    cust_total_debits:   float = 0.0
    cust_total_credits:  float = 0.0
    ws_total_debits:     float = 0.0
    ws_total_credits:    float = 0.0

    # WS category breakdown (from scheme-level Total row)
    ws_buy_sell:         float = 0.0   # Buy/Sell Amount (buy neg, sell pos)
    ws_income:           float = 0.0   # Dividends, interest
    ws_expenses:         float = 0.0   # Custodian charges
    ws_dep_with:         float = 0.0   # Investor deposits/withdrawals

    # Pending settlement (equity sell proceeds + MF orders)
    mf_orders_pending:   float = 0.0   # Pending settlement amount (from CN net amounts)
    ws_adjusted_closing: float = 0.0   # WS closing minus pending sell proceeds

    # Cross-check: opening + net transactions should equal closing
    cust_computed_closing: float = 0.0
    ws_computed_closing:   float = 0.0
    cust_cross_check_ok:   bool  = True
    ws_cross_check_ok:     bool  = True
    # Source file citations (for break investigation)
    cust_source_file:    str  = ''   # Bank statement file path
    ws_source_file:      str  = ''   # WS Bank Book file path
    ws_opening_source:   str  = ''   # Prior-day WS Bank Book file path
    cust_opening_source: str  = ''   # Prior-day bank file path

    ws_accounts:         List[str]   = field(default_factory=list)
    ws_account_details:  List[dict]  = field(default_factory=list)

    # Layer 1
    l1_variance:    float   = 0.0
    l1_status:      str     = ''    # MATCH | SHORTFALL | EXCESS | NO BALANCE DATA

    # Layer 2
    artificial_accounts: List[dict] = field(default_factory=list)
    artificial_total:    float      = 0.0
    l2_net:              float      = 0.0
    l2_status:           str        = ''   # COVERED | NETTED BREAK | N/A

    # Layer 3
    txn_matches: List[TxnMatchResult] = field(default_factory=list)

    # Un-booked sell annotation (post-classification pass)
    note:                 str   = ''    # Pool-level explanation, e.g. "Sell proceeds not booked in WS..."
    unbooked_sell_count:  int   = 0
    unbooked_sell_amount: float = 0.0

    @property
    def has_break(self) -> bool:
        return self.overall_status in ('Balance Break', 'Transaction Break')

    @property
    def unmatched_txn_count(self) -> int:
        """How many of today's transactions couldn't be reconciled 1:1.
        Used as a Note-level footnote when the primary break is a closing
        balance mismatch — txn-level non-alignment is a supporting detail,
        not a standalone headline.
        """
        if not self.has_opening_balance:
            return 0
        return sum(1 for t in self.txn_matches if not t.is_matched)

    @property
    def overall_status(self) -> str:
        # Status vocabulary (6 terms):
        #   Clean              — closing balances match AND txns reconcile
        #   Settlement Timing  — break equals pending settlement (equity sell / MF)
        #   Balance Break      — closing balances differ beyond TOLERANCE
        #                        (today's recon failed; the primary finding)
        #   Transaction Break  — closing matches but individual txns don't
        #                        reconcile (footnote-level finding)
        #   Not in WS          — custodian account has no corresponding WS Bank Book
        #   No Statement       — WS Bank Book exists but no custodian statement

        # Both sides zero with no transactions → Clean
        if (self.cust_closing == 0.0 and self.ws_closing_sum == 0.0
                and not self.txn_matches):
            if self.ws_accounts:
                return 'Clean'
            return 'Not in WS'

        # No WS accounts but custodian has a balance → not yet set up in WS
        if not self.ws_accounts and self.cust_closing != 0.0:
            return 'Not in WS'

        txn_break = self.has_opening_balance and any(
            not t.is_matched for t in self.txn_matches)

        # L1 MATCH: closing balances agree within TOLERANCE — always Clean.
        if self.l1_status == 'MATCH' and not txn_break:
            return 'Clean'

        # L2 COVERED: L1 missed but artificial accounts explain the gap → Clean
        if self.l2_status == 'COVERED':
            return 'Clean'

        # Settlement timing: adjust WS closing by pending settlement amount.
        if (self.mf_orders_pending > 0
                and abs(self.l1_variance) > TOLERANCE):
            _adjusted_var = abs(self.l1_variance) - self.mf_orders_pending
            if abs(_adjusted_var) <= TOLERANCE:
                return 'Settlement Timing'

        # L2 NETTED BREAK: variance persists after netting artificial accounts
        if self.l2_status == 'NETTED BREAK':
            return 'Balance Break'

        # No custodian statement received — informational, not a reconcilable break
        if self.l1_status == 'NO BALANCE DATA':
            return 'No Statement'

        # L1 balance mismatch beyond tolerance — primary break for the day.
        if self.l1_status != 'MATCH':
            return 'Balance Break'

        # Closing matches but individual transactions don't reconcile.
        return 'Transaction Break'

    def to_dict(self) -> dict:
        return {
            'mapid':               self.mapid,
            'strategy_name':       self.strategy_name,
            'bank':                self.bank,
            'cust_account':        self.cust_account,
            'cust_opening':        self.cust_opening,
            'ws_opening_sum':      self.ws_opening_sum,
            'has_opening_balance': self.has_opening_balance,
            'cust_closing':        self.cust_closing,
            'ws_closing_sum':      self.ws_closing_sum,
            'cust_total_debits':   self.cust_total_debits,
            'cust_total_credits':  self.cust_total_credits,
            'ws_total_debits':     self.ws_total_debits,
            'ws_total_credits':    self.ws_total_credits,
            'ws_buy_sell':         self.ws_buy_sell,
            'ws_income':           self.ws_income,
            'ws_expenses':         self.ws_expenses,
            'ws_dep_with':         self.ws_dep_with,
            'mf_orders_pending':   self.mf_orders_pending,
            'ws_adjusted_closing': self.ws_adjusted_closing,
            'cust_computed_closing': self.cust_computed_closing,
            'ws_computed_closing':   self.ws_computed_closing,
            'cust_cross_check_ok':   self.cust_cross_check_ok,
            'ws_cross_check_ok':     self.ws_cross_check_ok,
            'cust_source_file':    self.cust_source_file,
            'ws_source_file':      self.ws_source_file,
            'ws_opening_source':   self.ws_opening_source,
            'cust_opening_source': self.cust_opening_source,
            'ws_accounts':         self.ws_accounts,
            'ws_account_details':  self.ws_account_details,
            'l1_variance':         self.l1_variance,
            'l1_status':           self.l1_status,
            'artificial_accounts': self.artificial_accounts,
            'artificial_total':    self.artificial_total,
            'l2_net':              self.l2_net,
            'l2_status':           self.l2_status,
            'overall_status':      self.overall_status,
            'txn_matches':         [t.to_dict() for t in self.txn_matches],
            'note':                 self.note,
            'unbooked_sell_count':  self.unbooked_sell_count,
            'unbooked_sell_amount': self.unbooked_sell_amount,
        }


# ── Artificial account summary ────────────────────────────────────────────── #

@dataclass
class ArtificialAccountSummary:
    bank:          str
    account_no:    str
    balance:       float
    allocated_to:  List[str]   # MAPIDs that drew on this float to cover shortfall
    unallocated:   float       # remaining float not used by any shortfall

    def to_dict(self) -> dict:
        return {
            'bank':         self.bank,
            'account_no':   self.account_no,
            'balance':      self.balance,
            'allocated_to': self.allocated_to,
            'unallocated':  self.unallocated,
        }


# ── Top-level summary ─────────────────────────────────────────────────────── #

@dataclass
class BankReconSummary:
    date:                   str
    pool_results:           List[PoolReconResult]         = field(default_factory=list)
    artificial_summaries:   List[ArtificialAccountSummary] = field(default_factory=list)
    ws_only_accounts:       List[dict]                    = field(default_factory=list)
    cust_only_accounts:     List[dict]                    = field(default_factory=list)

    @property
    def total_pools(self) -> int:
        return len(self.pool_results)

    @property
    def clean(self) -> int:
        return sum(1 for r in self.pool_results if r.overall_status == 'Clean')

    @property
    def covered(self) -> int:
        return 0   # 'Covered by Float' merged into Clean

    @property
    def breaks(self) -> int:
        return sum(1 for r in self.pool_results
                   if r.overall_status in ('Balance Break', 'Transaction Break'))

    def to_dict(self) -> dict:
        return {
            'date':                 self.date,
            'total_pools':          self.total_pools,
            'clean':                self.clean,
            'covered':              self.covered,
            'breaks':               self.breaks,
            'pool_results':         [r.to_dict() for r in self.pool_results],
            'artificial_summaries': [a.to_dict() for a in self.artificial_summaries],
            'ws_only_accounts':     self.ws_only_accounts,
            'cust_only_accounts':   self.cust_only_accounts,
        }


# ── Engine ────────────────────────────────────────────────────────────────── #

class BankVsWSReconEngine:

    def reconcile(
        self,
        custodian_accounts,
        ws_book,
        pool_master,
        icici_pool_map:       dict = None,
        kotak_pool_map:       dict = None,
        hdfc_pool_map:        dict = None,
        client_bank_details         = None,
        date:                 str  = '',
        bank_balance_history: dict = None,
        ws_opening_history:   dict = None,
        mf_orders_by_mapid:   dict = None,
    ) -> BankReconSummary:
        """
        Three-layer Bank vs WS reconciliation.

        WS account resolution priority:
          1. client_bank_details  (Z13_ClientDPBankDetails.xlsx) — direct lookup
          2. WS scheme context    (scheme_name → MAPID via Pool Master) — authoritative
             for multi-strategy investors whose same account appears under multiple schemes
          3. custodian_acct NSDL col  — fallback where available
          (scheme-name partial-match fallback removed — client file is complete)
        """
        icici_pool_map = icici_pool_map or {}
        summary        = BankReconSummary(date=date)

        # ── Step 1: Scheme-name → MAPID lookup ───────────────────────────
        # Primary: from Pool Master (scheme names from Z13 pool master file)
        # Supplementary: ws_scheme_names from pool_map.json (WS bankbook exact names)
        # This ensures 'Mystic Wealth Value Portfolio Kotak' → 204496800 even though
        # the pool master stores 'GOLDSTANDARD WEALTH PVT LTD MYSTIC WEVA'.
        name_to_mapid = {}
        for mapid, info in pool_master.mapid_to_info.items():
            name = info.get('name', '').lower().strip()
            if name:
                name_to_mapid[name] = mapid
        # Supplement with ws_scheme_names from pools_hub.json
        try:
            from core.pools_hub import PoolsHub as _PHws
            for _wsn, _mid in _PHws.load().ws_scheme_names_index().items():
                name_to_mapid.setdefault(_wsn, _mid)
        except Exception:
            pass   # pools_hub.json not available — fall back to pool master only

        # ── Step 2: Classify custodian accounts ───────────────────────────
        pool_accounts = {}   # (bank, mapid) -> BankAccount
        artificial    = {}   # bank -> [BankAccount]

        for ca in custodian_accounts:
            bank  = ca.source.upper()
            mapid = self._resolve_cust_mapid(ca, bank, pool_master, icici_pool_map,
                                             client_bank_details=client_bank_details,
                                             kotak_pool_map=kotak_pool_map or {},
                                             hdfc_pool_map=hdfc_pool_map or {})
            if mapid:
                key = (bank, mapid)
                if key in pool_accounts:
                    existing = pool_accounts[key]
                    # Multiple accounts for same (bank, mapid) can mean two things:
                    #
                    # A) SAME account, multiple dates (Axis sends one zip per day for a
                    #    range) → MERGE: keep latest closing balance, earliest opening,
                    #    accumulate all transactions.
                    #
                    # B) DIFFERENT accounts, same pool (not expected but handled safely)
                    #    → SUM closing balances and merge transactions.
                    #
                    # Distinguish by comparing account_no.
                    if ca.account_no == existing.account_no:
                        # Same account, different date snapshot — merge correctly
                        from datetime import datetime
                        def _parse_dt(s):
                            for fmt in ('%d-%b-%Y', '%d-%m-%Y', '%Y-%m-%d'):
                                try: return datetime.strptime(str(s).upper(), fmt)
                                except: pass
                            return datetime.min

                        ca_dt  = _parse_dt(ca.as_on_date)
                        ex_dt  = _parse_dt(existing.as_on_date)

                        if ca_dt > ex_dt:
                            # ca is newer → use its closing balance and date
                            existing.closing_balance = ca.closing_balance
                            existing.as_on_date      = ca.as_on_date
                        # Always take the earliest opening balance
                        if ca_dt < ex_dt and ca.has_opening_balance and ca.opening_balance:
                            existing.opening_balance = ca.opening_balance
                        # Accumulate transactions from all dates
                        existing.transactions += ca.transactions
                    else:
                        # Different account numbers mapping to the same pool — sum balances
                        existing.closing_balance = round(
                            existing.closing_balance + ca.closing_balance, 2)
                        existing.opening_balance = round(
                            existing.opening_balance + ca.opening_balance, 2)
                        existing.transactions += ca.transactions
                else:
                    ca._mapid = mapid
                    pool_accounts[key] = ca
            else:
                artificial.setdefault(bank, []).append(ca)
                logger.info(f'Artificial / unresolved custodian account: [{bank}] {ca.account_no}  balance={ca.closing_balance:,.2f}')
                # Any unresolved custodian account with a non-zero balance
                # is a BREAK — it has real money but no WS pool to match to.
                if round(ca.closing_balance, 2) != 0.0:
                    summary.cust_only_accounts.append({
                        'bank':        bank,
                        'account_no':  ca.account_no,
                        'balance':     ca.closing_balance,
                        'status':      'BREAK',
                        'description': f'Custodian account {ca.account_no} has balance '
                                       f'{ca.closing_balance:,.2f} but is not mapped to '
                                       f'any WS pool. Add to hdfc/icici/kotak_bank_pool_map.json '
                                       f'or update WS bankbook.',
                    })

        # ── Step 3: Resolve WS accounts → (bank, mapid) ───────────────────
        #
        # For each WS account entry (ws_account, scheme_name):
        #   - client_bank_details gives the investor's primary MAPID (definitive)
        #   - But WS scheme context overrides for multi-strategy investors
        #     (same account under two schemes → use scheme_name → MAPID for each entry)
        #   - Result: each WS entry is attributed to exactly ONE (bank, mapid) pair
        #
        # Two pools:
        #   pool_linked_ws  — WS accounts with a direct client-file MAPID
        #                     used for Layer 1 (balance) + Layer 3 (transactions)
        #   ws_unresolved   — no client file entry AND no scheme resolution
        #
        # BALANCE grouping: use WS scheme context (scheme_name → MAPID)
        #   because multi-strategy investors split their balance between schemes
        #
        # WS accounts where client MAPID != scheme MAPID are flagged as
        # multi-strategy and attributed to the scheme MAPID for balance purposes.

        ws_by_pool:  Dict[Tuple[str, str], list] = {}
        ws_unresolved = []

        for wa in ws_book.accounts:
            if wa.bank_prefix in ('DUMMY', ''):
                continue

            scheme_name_lower = wa.scheme_name.lower().strip()

            # 1. NSDL depository column — direct pool linkage, highest authority
            nsdl_mapid = None
            if wa.custodian_acct:
                nsdl_mapid = pool_master.mapid_from_nsdl(wa.custodian_acct)
            if nsdl_mapid:
                mapid = nsdl_mapid
                wa.mapid = mapid
                ws_by_pool.setdefault((wa.bank_prefix, mapid), []).append(wa)
                continue

            # 2. CBD (Z13_ClientDPBankDetails) — maps account to custodian pool
            client_mapid = None
            if client_bank_details and client_bank_details.is_loaded():
                client_mapid = client_bank_details.mapid(wa.ws_account)

            # 3. Scheme name direct match
            scheme_mapid = name_to_mapid.get(scheme_name_lower)

            if scheme_mapid:
                # WS scheme context is authoritative for BALANCE grouping.
                # When WS places the same bank account under two different scheme
                # sections (e.g. ICICI-002101019475 under both Code 80/MYSTICV and
                # Code 81/MYSTICM), the scheme name is the only correct signal —
                # CBD will point to whichever pool it first encountered for this
                # shared account and cannot distinguish the two.
                mapid = scheme_mapid
            elif client_mapid:
                # Scheme name unknown — fall back to CBD
                mapid = client_mapid
            else:
                mapid = None

            wa.mapid = mapid or ''
            if mapid:
                ws_by_pool.setdefault((wa.bank_prefix, mapid), []).append(wa)
            else:
                ws_unresolved.append(wa)

        # Step 3b removed: the bank recon workflow now handles opening
        # balances for all banks (prev WD closing). The engine no longer
        # overrides with bank_balance_history which used the old B/F logic.

        # ── Step 3c: Build scheme summary index by MAPID ─────────────────
        # The WS Bank Book "Total" row per scheme gives us aggregate closing
        # balance + category breakdowns (Buy/Sell, Income, Expenses, Dep/With).
        # This replaces per-client account summing for financial comparison.
        ws_summary_by_mapid: Dict[str, 'WSSchemeSummary'] = {}
        if hasattr(ws_book, 'scheme_summaries') and ws_book.scheme_summaries:
            for ss in ws_book.scheme_summaries:
                sn_lower = ss.scheme_name.lower().strip()
                mid = name_to_mapid.get(sn_lower)
                if mid:
                    ws_summary_by_mapid[mid] = ss
                else:
                    logger.debug(f'WS scheme summary unresolved: {ss.scheme_name}')

        # ── Step 4: Match pool accounts → WS account groups ───────────────
        matched_keys = set()

        for (bank, mapid), ca in pool_accounts.items():
            # ALL WS accounts for this MAPID across ALL banks — used for
            # metadata (account list, client count) and Layer 3 transaction
            # matching.  Financial comparison now uses scheme summaries.
            # from ALL clients in that strategy regardless of which retail bank each
            # client uses. The WS Bank Book tracks each client at their own bank.
            # Summing ALL WS MAPID accounts gives the correct counterpart balance.
            # Individual WS accounts for this MAPID — used for metadata
            all_mapid_ws = [
                wa for (b, m), walist in ws_by_pool.items()
                if m == mapid for wa in walist
            ]

            # ── WS financials: prefer scheme summary over per-client sum ──
            ss = ws_summary_by_mapid.get(mapid)
            if ss:
                ws_balance_sum     = ss.closing_balance
                ws_buy_sell        = ss.buy_sell
                ws_income          = ss.income
                ws_expenses        = ss.expenses
                ws_dep_with        = ss.dep_with
                ws_total_debits    = ss.total_debits
                ws_total_credits   = ss.total_credits
            else:
                # Fallback: sum individual accounts (no scheme summary available)
                ws_balance_sum = round(sum(a.closing_balance for a in all_mapid_ws), 2)
                ws_buy_sell = ws_income = ws_expenses = ws_dep_with = 0.0
                ws_total_debits = round(abs(sum(
                    sum(t.amount for t in a.transactions if t.amount < 0)
                    for a in all_mapid_ws)), 2)
                ws_total_credits = round(sum(
                    sum(t.amount for t in a.transactions if t.amount > 0)
                    for a in all_mapid_ws), 2)

            has_cust_balance = ca.closing_balance != 0.0
            has_ws_data = bool(ss) or bool(all_mapid_ws)

            if not has_cust_balance or not has_ws_data:
                l1_variance = 0.0
                l1_status   = 'NO BALANCE DATA'
                l2_status   = 'N/A'
                l2_net      = 0.0
                art_accounts= []
                art_total   = 0.0
            else:
                l1_variance = round(ca.closing_balance - ws_balance_sum, 2)
                if abs(l1_variance) <= TOLERANCE:
                    l1_status = 'MATCH'
                elif l1_variance > 0:
                    l1_status = 'SHORTFALL'
                else:
                    l1_status = 'EXCESS'

                art_accounts = artificial.get(bank, [])
                art_total    = round(sum(a.closing_balance for a in art_accounts), 2)

                if l1_status == 'MATCH':
                    l2_status = 'N/A'
                    l2_net    = 0.0
                else:
                    l2_net    = round(l1_variance + art_total, 2)
                    l2_status = 'COVERED' if abs(l2_net) <= TOLERANCE else 'NETTED BREAK'

            # ── Opening balances ──
            cust_opening = ca.opening_balance

            # WS opening: prefer scheme summary from prior day, else
            # ws_opening_history (per-MAPID), else sum individual accounts
            if ws_opening_history and mapid in ws_opening_history:
                ws_opening_sum = ws_opening_history[mapid]
            elif ss:
                ws_opening_sum = ss.opening_balance
            else:
                ws_opening_sum = round(sum(a.opening_balance for a in all_mapid_ws), 2)

            # ── Transaction totals ──
            cust_total_debits  = ca.total_debits
            cust_total_credits = ca.total_credits

            # ── Cross-check: opening + net txns = closing? ──
            cust_net = round(cust_total_credits - cust_total_debits, 2)
            cust_computed_closing = round(cust_opening + cust_net, 2)
            cust_cross_check_ok = abs(cust_computed_closing - ca.closing_balance) <= TOLERANCE

            ws_net = round(ws_total_credits - ws_total_debits, 2)
            ws_computed_closing = round(ws_opening_sum + ws_net, 2)
            ws_cross_check_ok = abs(ws_computed_closing - ws_balance_sum) <= TOLERANCE

            if not cust_cross_check_ok and ca.has_opening_balance:
                logger.warning(
                    f'Cross-check FAIL [{mapid}] Bank: '
                    f'opening={cust_opening} + net={cust_net} = {cust_computed_closing} '
                    f'≠ closing={ca.closing_balance}')
            if not ws_cross_check_ok and all_mapid_ws:
                logger.warning(
                    f'Cross-check FAIL [{mapid}] WS: '
                    f'opening={ws_opening_sum} + net={ws_net} = {ws_computed_closing} '
                    f'≠ closing={ws_balance_sum}')

            # Layer 3: transaction matching.
            if not ca.has_opening_balance:
                txn_matches = []
            else:
                txn_matches = self._match_transactions(ca, all_mapid_ws, mapid)

            summary.pool_results.append(PoolReconResult(
                mapid               = mapid,
                strategy_name       = pool_master.strategy_name(mapid),
                bank                = bank,
                cust_account        = ca.account_no,
                cust_opening        = cust_opening,
                ws_opening_sum      = ws_opening_sum,
                has_opening_balance = getattr(ca, 'has_opening_balance', True),
                cust_closing        = ca.closing_balance,
                ws_closing_sum      = ws_balance_sum,
                cust_total_debits   = cust_total_debits,
                cust_total_credits  = cust_total_credits,
                ws_total_debits     = ws_total_debits,
                ws_total_credits    = ws_total_credits,
                ws_buy_sell         = ws_buy_sell,
                ws_income           = ws_income,
                ws_expenses         = ws_expenses,
                ws_dep_with         = ws_dep_with,
                mf_orders_pending   = (mf_orders_by_mapid or {}).get(mapid, 0.0),
                ws_adjusted_closing = round(ws_balance_sum - (mf_orders_by_mapid or {}).get(mapid, 0.0), 2),
                cust_computed_closing = cust_computed_closing,
                ws_computed_closing   = ws_computed_closing,
                cust_cross_check_ok   = cust_cross_check_ok,
                ws_cross_check_ok     = ws_cross_check_ok,
                cust_source_file    = getattr(ca, 'source_file', ''),
                ws_source_file      = getattr(ss, 'source_file', '') if ss else '',
                ws_accounts         = [a.ws_account for a in all_mapid_ws],
                ws_account_details  = [
                    {'ws_account': a.ws_account, 'scheme_name': a.scheme_name,
                     'bank_prefix': a.bank_prefix,
                     'opening_balance': a.opening_balance,
                     'closing_balance': a.closing_balance, 'mapid': a.mapid}
                    for a in all_mapid_ws
                ],
                l1_variance         = l1_variance,
                l1_status           = l1_status,
                artificial_accounts = [{'account_no': a.account_no, 'balance': a.closing_balance}
                                        for a in art_accounts],
                artificial_total    = art_total,
                l2_net              = l2_net,
                l2_status           = l2_status,
                txn_matches         = txn_matches,
            ))
            matched_keys.add((bank, mapid))

        # ── Step 5: WS accounts with no custodian pool ────────────────────
        # KEY: if a MAPID already has a matched custodian pool (e.g. Eterna → Axis),
        # its WS balances are ALREADY included in that pool row's ws_closing_sum
        # (which sums ALL investor bank accounts for the MAPID regardless of bank).
        # Do NOT create additional ws_only rows for those per-bank WS entries —
        # that would double-count the WS balance.
        # Only create ws_only rows for MAPIDs with NO matched custodian pool at all.
        all_matched_mapids = {mapid for _, mapid in matched_keys}

        # Group unmatched WS entries by MAPID (not by bank+mapid) so we get
        # ONE row per strategy rather than one row per investor bank.
        from collections import defaultdict as _dd5
        _ws_only_by_mapid: dict = _dd5(lambda: {'banks': set(), 'accounts': [], 'ws_sum': 0.0})
        for (bank, mapid), ws_accts in ws_by_pool.items():
            if mapid not in all_matched_mapids:
                # This MAPID has no custodian pool anywhere — truly unmatched
                entry = _ws_only_by_mapid[mapid]
                entry['banks'].add(bank)
                entry['accounts'].extend(a.ws_account for a in ws_accts)
                entry['ws_sum'] = round(entry['ws_sum'] + sum(a.closing_balance for a in ws_accts), 2)
            # else: MAPID IS matched — these WS accounts are already in the pool row; skip

        for mapid, entry in _ws_only_by_mapid.items():
            banks_str = '/'.join(sorted(entry['banks']))
            summary.ws_only_accounts.append({
                'bank':     banks_str,
                'mapid':    mapid,
                'strategy': pool_master.strategy_name(mapid),
                'accounts': entry['accounts'],
                'ws_sum':   entry['ws_sum'],
                'reason':   'No custodian pool account found for this strategy',
            })
        for wa in ws_unresolved:
            summary.ws_only_accounts.append({
                'bank':     wa.bank_prefix,
                'mapid':    '',
                'strategy': 'UNRESOLVED — not in client bank details or Pool Master',
                'accounts': [wa.ws_account],
                'ws_sum':   wa.closing_balance,
                'reason':   'Account not in client bank details file',
            })

        # ── Step 5b: surface ws_only_accounts in pool_results ─────────────
        # Strategies in the WS book but with no custodian file are tracked in
        # ws_only_accounts but were invisible in the UI (pool_results only shown).
        # Add them as NOT IN WS entries so the recon table shows them.
        for ws_entry in summary.ws_only_accounts:
            summary.pool_results.append(PoolReconResult(
                mapid               = ws_entry.get('mapid', ''),
                strategy_name       = ws_entry.get('strategy', ''),
                bank                = ws_entry.get('bank', ''),
                cust_account        = '',
                cust_opening        = 0.0,
                ws_opening_sum      = 0.0,
                has_opening_balance = False,
                cust_closing        = 0.0,
                ws_closing_sum      = ws_entry.get('ws_sum', 0.0),
                ws_accounts         = ws_entry.get('accounts', []),
                l1_variance         = -ws_entry.get('ws_sum', 0.0),
                l1_status           = 'NO BALANCE DATA',
                l2_status           = 'N/A',
            ))

        # ── Step 6: Artificial account summaries ──────────────────────────
        for bank, art_list in artificial.items():
            allocations     = [r.mapid for r in summary.pool_results
                               if r.bank == bank and r.l2_status == 'COVERED']
            total_art       = round(sum(a.closing_balance for a in art_list), 2)
            total_shortfall = round(sum(abs(r.l1_variance) for r in summary.pool_results
                                        if r.bank == bank and r.l2_status == 'COVERED'), 2)
            for a in art_list:
                summary.artificial_summaries.append(ArtificialAccountSummary(
                    bank=bank, account_no=a.account_no, balance=a.closing_balance,
                    allocated_to=allocations,
                    unallocated=round(total_art - total_shortfall, 2),
                ))

        # Count cust_only BREAK accounts against the overall break tally
        cust_only_breaks = sum(1 for c in summary.cust_only_accounts if c.get('status') == 'BREAK')

        # ── Annotate un-booked sells ──────────────────────────────────────
        self._annotate_unbooked_sells(summary)

        logger.info(
            f'BankVsWS Recon: {summary.total_pools} pools — '
            f'{summary.clean} CLEAN, {summary.covered} COVERED, {summary.breaks} BREAK'
            f' (incl. {cust_only_breaks} unresolved custodian account(s))'
        )
        return summary

    def _annotate_unbooked_sells(self, summary: 'BankReconSummary') -> None:
        """Flag CUST-ONLY credits as un-booked sell proceeds.

        A custodian credit with no matching WS entry is the signature of a
        sell that has settled in the pool account but has not yet been
        booked in WS — the bank-recon mirror of the holdings-recon
        un-booked-sell annotation.

        For each pool with a break (SHORTFALL = cust > ws), walks the
        txn_matches list, marks each CUST ONLY credit with a note, and
        rolls up a pool-level summary noting how much of the variance is
        explained by these un-booked sells.
        """
        for pr in summary.pool_results:
            if pr.overall_status in ('Clean', 'Settlement Timing',
                                     'No Statement', 'Not in WS'):
                continue
            pool_unbooked = 0
            pool_amount   = 0.0
            for tm in pr.txn_matches:
                if tm.status != 'CUST ONLY':
                    continue
                if tm.cust_amount is None or tm.cust_amount <= 0:
                    continue  # only credits are un-booked sell proceeds
                tm.unbooked_sell = True
                tm.note = 'Sell proceeds not booked in WS'
                pool_unbooked += 1
                pool_amount   += tm.cust_amount
            if pool_unbooked == 0:
                continue
            pr.unbooked_sell_count  = pool_unbooked
            pr.unbooked_sell_amount = round(pool_amount, 2)
            # If the un-booked credits roughly explain the SHORTFALL
            # variance, say so explicitly; otherwise just report the count.
            explains = (
                pr.l1_status == 'SHORTFALL'
                and abs(pool_amount - pr.l1_variance) <= max(TOLERANCE, abs(pr.l1_variance) * 0.01)
            )
            if explains:
                pr.note = (
                    f'Sell proceeds not booked in WS — {pool_unbooked} custodian '
                    f'credit(s) totalling {pool_amount:,.2f} explain the variance'
                )
            else:
                pr.note = (
                    f'{pool_unbooked} custodian credit(s) totalling '
                    f'{pool_amount:,.2f} not booked in WS (likely sell proceeds)'
                )
    # ------------------------------------------------------------------ #
    #  Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _resolve_cust_mapid(self, ca, bank: str, pool_master, icici_pool_map: dict,
                            client_bank_details=None, kotak_pool_map: dict = None,
                            hdfc_pool_map: dict = None) -> Optional[str]:
        """Resolve a custodian BankAccount to its Pool Master MAPID."""

        if bank == 'AXIS':
            c_group = getattr(ca, 'c_group', '') or ''
            if c_group:
                return c_group
            return None

        if bank == 'HDFC':
            # Priority 1: explicit pool map (hdfc_bank_pool_map.json).
            # This maps strategy pool accounts and NRI-without-WS accounts explicitly.
            # 'SKIP' means the account should not be reconciled (not live, UPI, no WS entry).
            if hdfc_pool_map and ca.account_no in hdfc_pool_map:
                mapped = hdfc_pool_map[ca.account_no]
                return None if mapped == 'SKIP' else mapped
            # Priority 2: CBD lookup — handles NRI accounts that ARE in WS bankbook
            # (e.g. HDFC-50100865254360 → Aristos NRO Mustafa).
            if client_bank_details and client_bank_details.is_loaded():
                ws_key = f'HDFC-{ca.account_no}'
                cbd_mapid = client_bank_details.mapid(ws_key)
                if cbd_mapid:
                    return cbd_mapid
            # Priority 3: zip_alias → Pool Master (fallback)
            zip_alias = getattr(ca, 'zip_alias', '') or getattr(ca, 'account_name', '') or ''
            return pool_master.resolve_custodian_account(
                bank='HDFC', account_no=ca.account_no, zip_alias=zip_alias)

        if bank == 'KOTAK':
            # Priority 1: direct pool map (kotak_bank_pool_map.json)
            # Maps pool account number → MAPID. Most reliable — no file parsing needed.
            if kotak_pool_map and ca.account_no in kotak_pool_map:
                return kotak_pool_map[ca.account_no]
            # Priority 2: kotak_client_id from companion XLSX (fragile but fallback)
            kotak_client = getattr(ca, 'kotak_client_id', '') or ''
            if kotak_client:
                return pool_master.resolve_custodian_account(
                    bank='KOTAK', account_no=ca.account_no, kotak_client_id=kotak_client)
            return None

        if bank == 'ICICI':
            return pool_master.resolve_custodian_account(
                bank='ICICI', account_no=ca.account_no, icici_pool_map=icici_pool_map)

        return None

    def _match_transactions(
        self,
        ca,             # BankAccount (custodian)
        ws_accounts,    # List[WSAccount] — ALL accounts for this MAPID (any bank)
        mapid: str,
    ) -> List[TxnMatchResult]:
        """
        Greedy transaction matcher: custodian txns vs WS txns for the same MAPID.

        Matching algorithm per custodian transaction:
          1. Find all unused WS entries on the same date with the same sign.
          2. Try exact 1:1 match first (single WS entry = custodian amount).
          3. If not, try N:1 match where Σ WS entries = custodian amount (greedy subset).
          4. Any WS entries left over after all custodian txns → WS ONLY.

        Matching is bank-agnostic: the custodian HDFC debit for NSE settlement
        will correctly match the WS RBL debit on the same date.
        """
        if not ca.transactions:
            # Still surface any WS transactions as WS ONLY
            return [
                TxnMatchResult(
                    date        = self._normalise_date(t.set_date),
                    cust_amount = 0.0, cust_desc='', cust_account='',
                    ws_amounts  = [t.amount], ws_descs=[t.description],
                    ws_accounts = [wa.ws_account for wa in ws_accounts
                                   for wt in wa.transactions if wt is t],
                    ws_sum=t.amount, variance=-t.amount, status='WS ONLY',
                )
                for wa in ws_accounts for t in wa.transactions
            ]

        # Index all WS transactions by date, tracking which account they belong to
        ws_by_date: Dict[str, List] = {}
        for wa in ws_accounts:
            for t in wa.transactions:
                dt = self._normalise_date(t.set_date)
                ws_by_date.setdefault(dt, []).append((t, wa.ws_account))

        ws_used: set = set()
        results: List[TxnMatchResult] = []

        for ct in ca.transactions:
            ct_date = ct.tran_date
            ct_amt  = round((-ct.debit if ct.debit else ct.credit), 2)
            ct_sign = -1 if ct_amt < 0 else 1

            candidates = [
                (t, acct) for t, acct in ws_by_date.get(ct_date, [])
                if id(t) not in ws_used
                and ((-1 if t.amount < 0 else 1) == ct_sign)
            ]

            if not candidates:
                results.append(TxnMatchResult(
                    date=ct_date, cust_amount=ct_amt, cust_desc=ct.description,
                    cust_account=ca.account_no,
                    ws_amounts=[], ws_descs=[], ws_accounts=[],
                    ws_sum=0.0, variance=ct_amt, status='CUST ONLY',
                ))
                continue

            # Try 1:1 exact match first
            exact = next(
                ((t, acct) for t, acct in candidates if abs(t.amount - ct_amt) <= TOLERANCE),
                None
            )
            if exact:
                t, acct = exact
                ws_used.add(id(t))
                results.append(TxnMatchResult(
                    date=ct_date, cust_amount=ct_amt, cust_desc=ct.description,
                    cust_account=ca.account_no,
                    ws_amounts=[t.amount], ws_descs=[t.description], ws_accounts=[acct],
                    ws_sum=round(t.amount, 2),
                    variance=round(ct_amt - t.amount, 2),
                    status='MATCHED',
                ))
                continue

            # N:1 greedy subset sum
            subset, running = [], 0.0
            for t, acct in candidates:
                if id(t) in ws_used:
                    continue
                subset.append((t, acct))
                running = round(running + t.amount, 2)
                if abs(running - ct_amt) <= TOLERANCE:
                    break

            ws_sum   = round(sum(t.amount for t, _ in subset), 2)
            variance = round(ct_amt - ws_sum, 2)

            if abs(variance) <= TOLERANCE:
                for t, _ in subset:
                    ws_used.add(id(t))
                status = 'MATCHED'
            else:
                status = 'PARTIAL'

            results.append(TxnMatchResult(
                date=ct_date, cust_amount=ct_amt, cust_desc=ct.description,
                cust_account=ca.account_no,
                ws_amounts=[t.amount for t, _ in subset],
                ws_descs  =[t.description for t, _ in subset],
                ws_accounts=[acct for _, acct in subset],
                ws_sum=ws_sum, variance=variance, status=status,
            ))

        # WS ONLY — transactions never matched to a custodian entry
        for wa in ws_accounts:
            for t in wa.transactions:
                if id(t) not in ws_used:
                    results.append(TxnMatchResult(
                        date=self._normalise_date(t.set_date),
                        cust_amount=0.0, cust_desc='', cust_account='',
                        ws_amounts=[t.amount], ws_descs=[t.description],
                        ws_accounts=[wa.ws_account],
                        ws_sum=t.amount, variance=-t.amount, status='WS ONLY',
                    ))

        return results

    @staticmethod
    def _normalise_date(s: str) -> str:
        """DD/MM/YYYY -> DD-MMM-YYYY  (to match custodian date format)"""
        import re
        MONTHS = ['JAN','FEB','MAR','APR','MAY','JUN',
                  'JUL','AUG','SEP','OCT','NOV','DEC']
        m = re.match(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})', s.strip())
        if m:
            dd, mm, yyyy = int(m.group(1)), int(m.group(2)), m.group(3)
            if 1 <= mm <= 12:
                return f'{dd:02d}-{MONTHS[mm-1]}-{yyyy}'
        return s
