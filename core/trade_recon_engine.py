"""
Trade Reconciliation Engine
-----------------------------
5-way reconciliation:
  Check 1 — WS Orders vs Dealer trades       (order fulfilment + price)
  Check 2 — Dealer trades vs Broker PDFs     (execution confirmation)
  Check 3 — Broker PDFs vs NSDL              (completeness)
  Check 4 — Generate WS 0096 output          (upload file)
  Check 5 — Exchange file validation         (post-facto, optional)
"""
import logging
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from parsers.dealer       import DealerTrade
from parsers.broker_pdf   import ContractNote, ContractNoteTrade, calculate_charges
from parsers.nsdl_steady  import NSDLRecord
from parsers.exchange_file import ExchangeTrade

logger = logging.getLogger(__name__)

PRICE_TOLERANCE = 0.0   # zero tolerance — prices must match exactly (to 4dp)


def _round_price(x) -> float:
    """Round a price to 4 decimals using half-away-from-zero (schoolbook)
    rounding, going through Decimal to avoid the FP artifacts that cause
    Python's built-in round() to give surprising results on values like
    982.07175 (which repr() displays as-is but internally is slightly
    under). Without this, 982.07175 rounds to 982.0717 while the CN
    value 982.0718 stays at 982.0718, producing a false price break."""
    if x is None:
        return 0.0
    try:
        from decimal import Decimal, ROUND_HALF_UP
        return float(Decimal(str(float(x))).quantize(
            Decimal('0.0001'), rounding=ROUND_HALF_UP))
    except Exception:
        return round(float(x), 4)


# ── Result data classes ───────────────────────────────────────────────────── #

@dataclass
class WSOrderAgg:
    """Aggregated WS orders for one pool × ISIN."""
    isin:            str
    instrument_code: str
    security_name:   str
    pool_account:    str   # dealer account code
    mapin:           str
    side:            str   # 'Buy' or 'Sell'
    total_qty:       float
    limit_price:     float  # 0 = market
    is_market:       bool
    trans_ids:       List[str] = field(default_factory=list)


@dataclass
class Check1Result:
    """WS Order vs Dealer fill check."""
    isin:            str
    instrument_code: str
    security_name:   str
    pool_account:    str
    mapin:           str
    side:            str
    ws_qty:          float
    dealer_fill_qty: float
    qty_diff:        float    # ws_qty - dealer_fill_qty (>0 = underfill)
    is_market:       bool
    limit_price:     float
    dealer_avg_px:   float
    price_breach:    bool
    status:          str      # MATCH | UNDERFILL | OVERFILL | PRICE_BREACH | DEALER_MISSING


@dataclass
class Check2Result:
    """Dealer trade vs Broker contract note check."""
    mapin:           str
    dealer_account:  str
    broker_code:     str
    isin:            str
    security_name:   str
    side:            str
    dealer_fill_qty: float
    dealer_avg_px:   float
    cn_qty:          float
    cn_wap:          float
    cn_no:           str
    qty_match:       bool
    price_match:     bool
    status:          str      # MATCH | QTY_BREAK | PRICE_BREAK | CN_MISSING | DEALER_MISSING
    cn_net_amount:   float = 0.0  # Net settlement amount from CN (negative=buy, positive=sell)


@dataclass
class Check3Result:
    """Broker PDF vs NSDL completeness check."""
    cn_no:            str
    isin:             str
    security_name:    str
    broker_sebi:      str
    broker_name:      str
    ucc:              str
    qty:              float
    net_rate:         float
    in_broker_pdf:    bool
    in_nsdl:          bool
    nsdl_status:      str    # ECN status if in NSDL
    status:           str    # MATCH | NSDL_MISSING | PDF_MISSING | LATE_REPORT


@dataclass
class Output0096Row:
    """One row in the WS 0096 upload file."""
    broker_code:       str   # SEBI registration number
    dummy:             int   # always 0
    security_code:     str   # WS instrument code
    exchange:          str
    transaction_type:  str   # BY- or SL+
    transaction_date:  str   # DD/MM/YYYY
    settlement_date:   str   # DD/MM/YYYY
    quantity:          float
    price:             float
    brokerage_per_share: float
    service_tax:       float  # 0
    settlement_flag:   str    # F
    market_rate:       float  # 0
    cash_symbol:       str    # CASH
    block_flag:        str    # Y
    stt:               float  # total STT in ₹ per row (not per share)
    accrued_interest:  float  # 0
    mapin_id:          str
    remarks:           str    # CN number
    cash_settlement_date: str
    stamp_duty:        float  # 0


@dataclass
class Check5Result:
    """Exchange file vs CN comparison (post-facto)."""
    security:          str
    isin:              str
    side:              str
    exchange:          str
    exchange_qty:      float
    exchange_avg_px:   float
    cn_qty:            float
    cn_avg_px:         float
    qty_diff:          float
    price_diff:        float
    status:            str    # MATCH | SIDE_UNVERIFIED | QTY_BREAK | PRICE_BREAK | EXCHANGE_ONLY | CN_ONLY


@dataclass
class TradeReconSummary:
    date:            str
    check1_results:  List[Check1Result]  = field(default_factory=list)
    check2_results:  List[Check2Result]  = field(default_factory=list)
    check3_results:  List[Check3Result]  = field(default_factory=list)
    output_0096:     List[Output0096Row] = field(default_factory=list)
    check5_results:  List[Check5Result]  = field(default_factory=list)
    warnings:        List[str]           = field(default_factory=list)

    # Counts
    @property
    def c1_breaks(self) -> int:
        # UNDERFILL = dealer filled less than WS order — informational, not actionable
        return sum(1 for r in self.check1_results
                   if r.status not in ('MATCH', 'UNDERFILL'))

    @property
    def c2_breaks(self) -> int:
        # UNFILLED = zero dealer fill, no CN expected — not a break
        return sum(1 for r in self.check2_results
                   if r.status not in ('MATCH', 'UNFILLED'))

    @property
    def c3_breaks(self) -> int:
        return sum(1 for r in self.check3_results if r.status != 'MATCH')

    @property
    def c5_breaks(self) -> int:
        # SIDE_UNVERIFIED = economic match with garbled CID side column, not a real break
        return sum(1 for r in self.check5_results
                   if r.status not in ('MATCH', 'SIDE_UNVERIFIED'))

    def to_dict(self) -> dict:
        def _r(obj):
            return {k: v for k, v in obj.__dict__.items()}
        return {
            'date':           self.date,
            'check1':         [_r(r) for r in self.check1_results],
            'check2':         [_r(r) for r in self.check2_results],
            'check3':         [_r(r) for r in self.check3_results],
            'output_0096':    [_r(r) for r in self.output_0096],
            'check5':         [_r(r) for r in self.check5_results],
            'warnings':       self.warnings,
            'c1_breaks':      self.c1_breaks,
            'c2_breaks':      self.c2_breaks,
            'c3_breaks':      self.c3_breaks,
            'c5_breaks':      self.c5_breaks,
            'total_0096_rows': len(self.output_0096),
        }


# ── Engine ────────────────────────────────────────────────────────────────── #

class TradeReconEngine:

    def __init__(self, broker_map: dict, pool_map: dict):
        """
        broker_map: loaded from config/broker_map.json
        pool_map:   loaded from config/pool_map.json
        """
        # Index by dealer_code, sebi_code, and full SEBI registration number
        self._brokers_by_dealer = {
            b['dealer_code'].upper(): b
            for b in broker_map.get('brokers', [])
        }
        self._brokers_by_sebi = {
            b['sebi_code'].upper(): b
            for b in broker_map.get('brokers', [])
        }
        # Also index by sebi_reg_no (full SEBI registration like INZ000166136)
        # CNs from NSDL files use the full reg number, not the short code
        self._brokers_by_reg_no = {
            b['sebi_reg_no'].upper(): b
            for b in broker_map.get('brokers', [])
            if b.get('sebi_reg_no')
        }
        # Index pool map by dealer_account
        self._pools = {
            p['dealer_account'].upper(): p
            for p in pool_map.get('pools', [])
        }
        # Index pool map by mapin (for reverse lookup)
        self._mapin_to_pool = {
            p['mapin'].upper(): p
            for p in pool_map.get('pools', [])
        }
        # Also index broker_cn_aliases so UCCs like GSWP012 (Haitong) resolve
        # to the same pool as the canonical mapin (GWPJ0004)
        for p in pool_map.get('pools', []):
            for alias in p.get('broker_cn_aliases', []):
                _alias_mapin = (alias.get('mapin') or '').upper()
                if _alias_mapin and _alias_mapin not in self._mapin_to_pool:
                    self._mapin_to_pool[_alias_mapin] = p
        # Index pool map by pool_name (OrderLog POOLNAME column → dealer_account)
        # Normalised: lower-case, spaces collapsed for fuzzy tolerance
        self._pool_name_to_pool = {
            p['pool_name'].strip().lower(): p
            for p in pool_map.get('pools', [])
            if p.get('pool_name')
        }

    def run(self,
            date_str:         str,
            ws_orders:        list,
            dealer_trades:    List[DealerTrade],
            contract_notes:   List[ContractNote],
            nsdl_records:     List[NSDLRecord],
            exchange_trades:  List[ExchangeTrade],
            cbd_client_map:   Dict[str, str],
            isin_to_instr:    Dict[str, str],
            sec_name_to_isin: Dict[str, str] = None,  # normalized company name → ISIN
            isin_to_name:       Dict[str, str] = None,  # ISIN → full security name (from Z8/OrderLog)
            isin_to_nse_ticker: Dict[str, str] = None,  # ISIN → NSE ticker symbol (from Z8, for 0096)
            ) -> TradeReconSummary:

        summary = TradeReconSummary(date=date_str)

        # ── Enrich dealer trades with Mapin ───────────────────────────────
        # Use canonical_mapin when pool entry is an alias (e.g. dealer_account
        # "GSWP_WEMO_ICICI" may resolve to alias entry with mapin=25281 rather
        # than the canonical MYSTICM entry, causing UCC exact-match to fail in C2).
        for dt in dealer_trades:
            pool = self._pools.get(dt.account.upper())
            if pool:
                dt.mapin = pool.get('canonical_mapin', pool['mapin'])
            else:
                dt.mapin = ''

        # ── Build reverse map: instrument_code → isin ─────────────────────
        # 0096 XLS files store WS instrument codes (e.g. EQ24662) not ISINs.
        # We reverse isin_to_instr so we can fill in CN trade ISINs.
        instr_to_isin = {v: k for k, v in isin_to_instr.items() if v and k}

        # ── Enrich CN trade ISINs from instrument codes ────────────────────
        # Also build a security-name→isin map as a secondary fallback
        # (Nuvama XLS uses ticker symbols like HCLTECH, DIVISLAB)
        security_to_isin: Dict[str, str] = {}
        for ws in ws_orders:
            isin = str(ws.get('ISINCODE', '') or '').strip()
            name = str(ws.get('INSTRUMENT_NAME', '') or '').strip().upper()
            code = str(ws.get('INSTRUMENT_CODE', '') or '').strip()
            if isin and name:
                security_to_isin[name] = isin
            if isin and code:
                instr_to_isin[code.upper()] = isin

        # Build dealer security→isin from dealer trades themselves
        for dt in dealer_trades:
            if dt.isin and dt.security:
                security_to_isin[dt.security.upper()] = dt.isin
                instr_to_isin[dt.security.upper()] = dt.isin

        for cn in contract_notes:
            for trade in cn.trades:
                if trade.isin:
                    continue  # already has ISIN
                # Try instrument code lookup (e.g. EQ24662 → INE121A01024)
                sec_up = trade.security_name.upper().strip()
                resolved = (instr_to_isin.get(sec_up) or
                            instr_to_isin.get(trade.security_name.strip()) or
                            security_to_isin.get(sec_up))
                if resolved:
                    trade.isin = resolved
                    logger.debug(f"CN ISIN resolved: {trade.security_name!r} → {resolved}")

        # ── CHECK 1: WS Orders vs Dealer ─────────────────────────────────
        summary.check1_results = self._check1_ws_vs_dealer(
            ws_orders, dealer_trades, cbd_client_map, isin_to_instr
        )
        summary.warnings.extend(self._c1_warnings(summary.check1_results))

        # ── Filter CNs to the reconciliation date ────────────────────────
        # The 72-hour email fetch window pulls CNs from multiple days.
        # Only CNs matching the recon date are relevant for Check 2 and 0096.
        # CNs from other dates are preserved separately for historical reference.
        from datetime import datetime
        try:
            recon_date_dt = datetime.strptime(date_str, '%Y-%m-%d')
            recon_date_fmt = recon_date_dt.strftime('%d/%m/%Y')
        except ValueError:
            recon_date_fmt = date_str

        def _cn_matches_date(cn: 'ContractNote') -> bool:
            if not cn.trade_date:
                return True  # no date — include (can't exclude)
            return cn.trade_date == recon_date_fmt

        contract_notes_today = [cn for cn in contract_notes if _cn_matches_date(cn)]
        contract_notes_other = [cn for cn in contract_notes if not _cn_matches_date(cn)]
        if contract_notes_other:
            other_dates = sorted(set(cn.trade_date for cn in contract_notes_other if cn.trade_date))
            summary.warnings.append(
                f"Excluded {len(contract_notes_other)} CNs from other dates "
                f"({', '.join(other_dates)}) — these are from the 72-hour email fetch window."
            )
            logger.info(
                f"Date filter: {len(contract_notes_today)} CNs match {recon_date_fmt}, "
                f"{len(contract_notes_other)} CNs from other dates excluded from matching"
            )

        # ── CHECK 2: Dealer vs Broker PDFs ────────────────────────────────
        # Deduplicate CNs before C2:
        # 1. Remove UNKNOWN CNs whose trades are covered by real-ECN CNs with same UCC
        # 2. Remove duplicate ECNs: same CN number from multiple sources (PDF + NSDL STeADY)
        #    Keep the one whose UCC is a canonical pool mapin; fall back to first seen.
        contract_notes_today = self._dedup_unknown_cns(list(contract_notes_today))
        contract_notes_today = self._dedup_ecn_duplicates(contract_notes_today)
        summary.check2_results = self._check2_dealer_vs_cn(
            dealer_trades, contract_notes_today
        )

        # ── CHECK 3: Broker PDFs vs NSDL ─────────────────────────────────
        summary.check3_results = self._check3_cn_vs_nsdl(
            contract_notes_today, nsdl_records,
            isin_to_name=isin_to_name or {}
        )

        # ── CHECK 4: Generate 0096 output ────────────────────────────────
        summary.output_0096 = self._generate_0096(
            contract_notes_today, dealer_trades, isin_to_instr, date_str,
            isin_to_nse_ticker=isin_to_nse_ticker or {}
        )

        # ── CHECK 5: Exchange validation (optional, post-facto) ───────────
        # Filter exchange trades to the recon date only.
        # Exchange PDFs cover the last N trading days; without this filter
        # a stale PDF from a prior run would silently contaminate today's C5.
        exchange_trades_today = [
            et for et in exchange_trades
            if not et.trade_date or et.trade_date == recon_date_fmt
        ]
        exchange_trades_stale = [
            et for et in exchange_trades
            if et.trade_date and et.trade_date != recon_date_fmt
        ]
        if exchange_trades_stale:
            stale_dates = sorted(set(et.trade_date for et in exchange_trades_stale))
            summary.warnings.append(
                f"Exchange: excluded {len(exchange_trades_stale)} trade(s) from other "
                f"date(s) {stale_dates} — delete stale exchange PDFs from raw/exchange/ "
                f"and re-fetch to resolve."
            )
            logger.warning(
                "Exchange date filter: %d trades match %s, %d from other dates excluded: %s",
                len(exchange_trades_today), recon_date_fmt,
                len(exchange_trades_stale), stale_dates
            )
        if exchange_trades_today:
            summary.check5_results = self._check5_exchange(
                exchange_trades_today, contract_notes_today, isin_to_instr,
                sec_name_to_isin or {}
            )

        total_cn = len(contract_notes)
        total_0096 = len(summary.output_0096)
        logger.info(
            f"Trade recon {date_str}: "
            f"C1={len(summary.check1_results)} ({summary.c1_breaks} breaks), "
            f"C2={len(summary.check2_results)} ({summary.c2_breaks} breaks), "
            f"C3={len(summary.check3_results)} ({summary.c3_breaks} breaks), "
            f"0096={total_0096} rows"
        )
        return summary

    # ── Check 1 ───────────────────────────────────────────────────────────── #

    def _check1_ws_vs_dealer(self, ws_orders: list,
                              dealer_trades: List[DealerTrade],
                              cbd_client_map: Dict[str, str],
                              isin_to_instr: Dict[str, str]) -> List[Check1Result]:
        """
        Aggregate WS orders by pool × ISIN × side and compare to dealer fills.
        Uses CBD to map CLIENT_CODE → Mapin/UCC → dealer pool account.
        """
        results = []

        # Build WS aggregation: (pool_account, isin, side) → agg
        ws_agg: Dict[Tuple, WSOrderAgg] = {}

        for order in ws_orders:
            client_code = str(order.get('CLIENT_CODE', '')).strip()
            isin        = str(order.get('ISINCODE', '')).strip()
            trxn_type   = str(order.get('TRXN_TYPE', '')).strip().upper()
            qty         = float(order.get('QUANTITY', 0) or 0)
            tran_price  = float(order.get('TRAN_PRICE', 0) or 0)
            instr_code  = str(order.get('INSTRUMENT_CODE', '')).strip()
            instr_name  = str(order.get('INSTRUMENT_NAME', '')).strip()
            trans_id    = str(order.get('TRANS_ID', '')).strip()

            if not isin or qty <= 0:
                continue

            # Map client → mapin
            mapin = cbd_client_map.get(client_code, '')
            if not mapin:
                mapin = client_code

            # Find dealer pool account via mapin first, then POOL_NAME fallback.
            # OrderLog BROKERACID can be a client-level account ID (e.g. MYSM00001)
            # that isn't directly in pool_map. In that case, the POOL_NAME column
            # (AQ in OrderLog) identifies which pool this client belongs to.
            pool = self._mapin_to_pool.get(mapin.upper(), {})
            if not pool:
                # POOL_NAME fallback (OrderLog POOL_NAME → pool_name index)
                order_pool_name = str(order.get('POOL_NAME', '') or '').strip().lower()
                if order_pool_name:
                    pool = self._pool_name_to_pool.get(order_pool_name, {})
                    if pool:
                        # Use the canonical mapin from pool_map, not the client ACID
                        mapin = pool.get('mapin', mapin)
            # Use canonical_mapin if pool entry is an alias (e.g. 25281 → MYSTICM)
            mapin = pool.get('canonical_mapin', mapin)
            pool_account = pool.get('dealer_account', mapin)

            side = 'Buy' if trxn_type == 'P' else 'Sell'
            key  = (pool_account.upper(), isin, side)

            if key not in ws_agg:
                ws_agg[key] = WSOrderAgg(
                    isin=isin,
                    instrument_code=isin_to_instr.get(isin, instr_code),
                    security_name=instr_name,
                    pool_account=pool_account,
                    mapin=mapin,
                    side=side,
                    total_qty=0.0,
                    # TRAN_PRICE is the WS execution price, NOT a limit price.
                    # We treat all WS orders as market orders for the purposes
                    # of Check 1 — the limit check uses the dealer's LmtPx instead.
                    limit_price=0.0,
                    is_market=True,
                    trans_ids=[],
                )
            ws_agg[key].total_qty += qty
            ws_agg[key].trans_ids.append(trans_id)

        # Build dealer aggregation: (pool_account, isin, side) → total fill + avg px + limit
        dealer_agg: Dict[Tuple, dict] = defaultdict(
            lambda: {'fill_qty': 0.0, 'value': 0.0, 'lmt_px': 0.0}
        )
        for dt in dealer_trades:
            key = (dt.account.upper(), dt.isin, dt.side.capitalize())
            dealer_agg[key]['fill_qty'] += dt.fill_qty
            dealer_agg[key]['value']    += dt.fill_qty * dt.avg_px
            if dt.lmt_px > 0:
                dealer_agg[key]['lmt_px'] = dt.lmt_px

        # Compare
        all_keys = set(ws_agg.keys()) | set(dealer_agg.keys())
        for key in sorted(all_keys):
            pool_account, isin, side = key
            ws   = ws_agg.get(key)
            deal = dealer_agg.get(key)

            ws_qty  = ws.total_qty if ws else 0.0
            ws_name = ws.security_name if ws else ''
            ws_instr= ws.instrument_code if ws else isin_to_instr.get(isin, '')
            mapin   = ws.mapin if ws else ''

            fill_qty = deal['fill_qty'] if deal else 0.0
            avg_px   = round(deal['value'] / fill_qty, 4) if (deal and fill_qty > 0) else 0.0
            # Use dealer's LmtPx (not WS TRAN_PRICE) for limit check
            lmt_px   = deal['lmt_px'] if deal else 0.0
            is_mkt   = (lmt_px == 0.0)

            qty_diff = round(ws_qty - fill_qty, 4)

            # Price breach check — only when dealer placed a limit order
            price_breach = False
            if not is_mkt and lmt_px > 0 and avg_px > 0:
                if side == 'Buy'  and avg_px > lmt_px + PRICE_TOLERANCE:
                    price_breach = True
                if side == 'Sell' and avg_px < lmt_px - PRICE_TOLERANCE:
                    price_breach = True

            # Status
            if not deal:
                status = 'DEALER_MISSING'
            elif not ws:
                status = 'WS_MISSING'
            elif fill_qty > ws_qty + 0.01:
                status = 'OVERFILL'
            elif price_breach:
                status = 'PRICE_BREACH'
            elif abs(qty_diff) > 0.01:
                status = 'UNDERFILL'
            else:
                status = 'MATCH'

            results.append(Check1Result(
                isin=isin,
                instrument_code=ws_instr,
                security_name=ws_name,
                pool_account=pool_account,
                mapin=mapin,
                side=side,
                ws_qty=ws_qty,
                dealer_fill_qty=fill_qty,
                qty_diff=qty_diff,
                is_market=is_mkt,
                limit_price=lmt_px,
                dealer_avg_px=avg_px,
                price_breach=price_breach,
                status=status,
            ))

        return results

    def _c1_warnings(self, results: List[Check1Result]) -> List[str]:
        warns = []
        for r in results:
            if r.status == 'OVERFILL':
                warns.append(
                    f"CRITICAL: {r.pool_account}/{r.isin} dealer filled {r.dealer_fill_qty} "
                    f"> WS order {r.ws_qty}. Possible error."
                )
            if r.status == 'PRICE_BREACH':
                warns.append(
                    f"PRICE BREACH: {r.pool_account}/{r.isin} limit={r.limit_price} "
                    f"but dealer AvgPx={r.dealer_avg_px} (side={r.side})"
                )
        return warns

    # ── Check 2 ───────────────────────────────────────────────────────────── #

    def _check2_dealer_vs_cn(self, dealer_trades: List[DealerTrade],
                              contract_notes: List[ContractNote]) -> List[Check2Result]:
        """Match dealer rows to broker contract notes.

        Matching strategy (in order):
        1. Exact: (ucc, dealer_code, isin, side)
        2. UCC-fallback: (dealer_code, isin, side) — when PDF UCC is blank/wrong
        3. ISIN-fallback: (ucc, dealer_code, side) — when ISIN enrichment failed
        4. Loose: (dealer_code, side) with qty check — last resort
        """
        results = []

        # Build all CN index levels
        cn_exact:     Dict[Tuple, List[ContractNote]] = defaultdict(list)  # (ucc, dealer, isin, side)
        cn_no_ucc:    Dict[Tuple, List[ContractNote]] = defaultdict(list)  # (dealer, isin, side)
        cn_no_isin:   Dict[Tuple, List[ContractNote]] = defaultdict(list)  # (ucc, dealer, side)

        for cn in contract_notes:
            for trade in cn.trades:
                broker      = (self._brokers_by_sebi.get(cn.broker_sebi.upper()) or
                               self._brokers_by_reg_no.get(cn.broker_sebi.upper()) or {})
                dealer_code = broker.get('dealer_code', cn.broker_sebi).upper()
                ucc         = cn.ucc.upper()
                isin        = trade.isin
                side        = trade.side.capitalize()

                cn_exact  [(ucc, dealer_code, isin, side)].append(cn)
                cn_no_ucc [(dealer_code, isin, side)      ].append(cn)
                cn_no_isin[(ucc, dealer_code, side)       ].append(cn)

                # Also index by the pool's canonical mapin so that a dealer row
                # keyed by pool mapin (GWPJ0004) finds a CN keyed by a broker-
                # specific UCC (GSWP012 for Haitong, INST22266 for Motilal).
                _pool_entry = self._mapin_to_pool.get(ucc, {})
                _pool_mapin = (_pool_entry.get('canonical_mapin') or
                               _pool_entry.get('mapin') or '').upper()
                if _pool_mapin and _pool_mapin != ucc:
                    cn_exact[(_pool_mapin, dealer_code, isin, side)].append(cn)
                    cn_no_isin[(_pool_mapin, dealer_code, side)     ].append(cn)

        def _canonical_ucc(ucc: str) -> str:
            """Resolve a UCC/MAPIN to its canonical pool mapin."""
            entry = self._mapin_to_pool.get(ucc.upper(), {})
            return entry.get('canonical_mapin', ucc).upper() or ucc.upper()

        def _find_cns(dt: 'DealerTrade') -> List[ContractNote]:
            side = dt.side.capitalize()
            dc   = dt.brkr_code.upper()
            ucc  = dt.mapin.upper()
            isin = dt.isin

            # 1. Exact match
            cns = cn_exact.get((ucc, dc, isin, side), [])
            if cns:
                return cns

            # 2. UCC-fallback (PDF may have blank/wrong UCC) — pool-filtered.
            # When two strategies trade the same ISIN via the same broker,
            # the no-UCC bucket contains CNs for ALL strategies. Filter to
            # only CNs whose UCC resolves to the same canonical pool as the
            # dealer trade's mapin, preventing cross-pool CN aggregation.
            cns = cn_no_ucc.get((dc, isin, side), [])
            if cns:
                dealer_canon = _canonical_ucc(ucc)
                pool_cns = [cn for cn in cns
                            if _canonical_ucc(cn.ucc) == dealer_canon]
                if pool_cns:
                    return pool_cns
                # No pool match — fall through to return all (single-strategy case)
                return cns

            # 3. ISIN-fallback (XLS script code not resolved to ISIN)
            cns = cn_no_isin.get((ucc, dc, side), [])
            if cns:
                # Verify by qty proximity to avoid false matches
                best = [cn for cn in cns
                        if any(abs(t.qty - dt.fill_qty) <= max(1, dt.fill_qty * 0.01)
                               for t in cn.trades)]
                return best or cns

            return []

        # Aggregate dealer trades by (mapin, broker, isin, side) before matching.
        # The dealer may have 2+ rows for same security; CN confirms aggregate qty.
        from collections import defaultdict as _dd2
        _dagg: dict = _dd2(lambda: {'qty': 0.0, 'value': 0.0, 'ref': None})
        for _dt in dealer_trades:
            _k = (_dt.mapin.upper(), _dt.brkr_code.upper(), _dt.isin, _dt.side.capitalize())
            _dagg[_k]['qty']   += _dt.fill_qty
            _dagg[_k]['value'] += _dt.fill_qty * _dt.avg_px
            if _dagg[_k]['ref'] is None: _dagg[_k]['ref'] = _dt

        class _AggDT:
            pass
        _agg_trades = []
        for (_m, _b, _i, _s), _v in _dagg.items():
            _r = _v['ref']
            _a = _AggDT()
            _a.mapin = _r.mapin; _a.account = _r.account; _a.brkr_code = _r.brkr_code
            _a.isin = _i; _a.name = _r.name; _a.security = _r.security
            _a.side = _s; _a.fill_qty = _v['qty']
            _a.avg_px = round(_v['value'] / _v['qty'], 6) if _v['qty'] > 0 else 0.0
            _agg_trades.append(_a)

        # Track CN trades already consumed by a dealer match to prevent
        # the same CN trade from being matched to multiple dealer rows
        # (e.g. two P N Gadgil BUYs at different prices for different pools).
        _used_cn_trades: set = set()  # set of id(trade) objects

        for dt in _agg_trades:
            side = dt.side.capitalize()

            # Zero-fill: order was placed but not executed — no CN expected.
            # Skip entirely — nothing to reconcile against a contract note.
            if dt.fill_qty == 0:
                continue

            matching_cns = _find_cns(dt)
            logger.info(
                f'C2 dealer→CN: mapin={dt.mapin} broker={dt.brkr_code} '
                f'isin={dt.isin} side={side} '
                f'qty={dt.fill_qty} px={dt.avg_px:.4f} → '
                f'{len(matching_cns)} CN(s): {[(cn.cn_no, cn.ucc) for cn in matching_cns]}')

            if not matching_cns:
                results.append(Check2Result(
                    mapin=dt.mapin, dealer_account=dt.account,
                    broker_code=dt.brkr_code, isin=dt.isin,
                    security_name=dt.name, side=side,
                    dealer_fill_qty=dt.fill_qty, dealer_avg_px=dt.avg_px,
                    cn_qty=0.0, cn_wap=0.0, cn_no='',
                    qty_match=False, price_match=False, status='CN_MISSING',
                ))
                continue

            # Collect all candidate CN trades for this ISIN + side,
            # excluding trades already consumed by a prior dealer match
            _cn_candidates = []
            for cn in matching_cns:
                for t in cn.trades:
                    if id(t) in _used_cn_trades:
                        continue
                    isin_match = (t.isin == dt.isin) if (t.isin and dt.isin) else True
                    side_match = t.side.capitalize() == side
                    if isin_match and side_match:
                        _cn_candidates.append((cn, t))

            if not _cn_candidates and matching_cns:
                # ISIN-fallback: any trade of matching side
                for cn in matching_cns:
                    for t in cn.trades:
                        if id(t) in _used_cn_trades:
                            continue
                        if t.side.capitalize() == side:
                            _cn_candidates.append((cn, t))

            # When multiple CN trades match (same ISIN, same side, same qty
            # but different prices — e.g. two P N Gadgil BUYs for different
            # pools), pick the one with closest price to the dealer's avg_px.
            if len(_cn_candidates) > 1 and dt.avg_px > 0:
                _cn_candidates.sort(
                    key=lambda ct: abs(ct[1].wap - dt.avg_px))
            logger.info(
                f'  Candidates after filter: {len(_cn_candidates)} — '
                f'{[(cn.cn_no, t.isin, t.wap, t.qty, "USED" if id(t) in _used_cn_trades else "avail") for cn, t in _cn_candidates]}')

            # Accumulate: take CN trades greedily by closest price until
            # qty is filled or all candidates exhausted
            total_cn_qty = 0.0
            cn_wap_sum   = 0.0
            cn_nos       = []
            _cn_net_sum  = 0.0
            _remaining_need = dt.fill_qty
            for cn, t in _cn_candidates:
                if _remaining_need <= 0.01:
                    break
                _use_qty = min(t.qty, _remaining_need)
                total_cn_qty += _use_qty
                cn_wap_sum   += _use_qty * t.wap
                _remaining_need -= _use_qty
                _used_cn_trades.add(id(t))
                if cn.cn_no not in cn_nos:
                    cn_nos.append(cn.cn_no)
                    # Add the CN's net settlement amount (one per CN)
                    _cn_net_sum += getattr(cn, 'net_amount', 0.0)

            cn_wap      = _round_price(cn_wap_sum / total_cn_qty) if total_cn_qty > 0 else 0.0
            qty_match   = abs(total_cn_qty - dt.fill_qty) <= 0.01
            price_match = _round_price(cn_wap) == _round_price(dt.avg_px)

            if qty_match and price_match:
                status = 'MATCH'
            elif not qty_match and not price_match:
                status = 'QTY_AND_PRICE_BREAK'
            elif not qty_match:
                status = 'QTY_BREAK'
            else:
                status = 'PRICE_BREAK'

            results.append(Check2Result(
                mapin=dt.mapin, dealer_account=dt.account,
                broker_code=dt.brkr_code, isin=dt.isin,
                security_name=dt.name, side=side,
                dealer_fill_qty=dt.fill_qty, dealer_avg_px=dt.avg_px,
                cn_qty=total_cn_qty, cn_wap=cn_wap,
                cn_no=', '.join(cn_nos),
                qty_match=qty_match, price_match=price_match, status=status,
                cn_net_amount=round(_cn_net_sum, 2),
            ))

        return results

    # ── Check 3 ───────────────────────────────────────────────────────────── #

    @staticmethod
    def _trunc_at_ltd(name: str) -> str:
        """
        Truncate a security name at the first occurrence of a corporate-suffix
        word (Ltd, Limited, Private, Corp, Inc, Co.) and strip trailing
        punctuation/spaces.  Generic words like 'Finance', 'Technologies',
        'Services' are intentionally NOT stripped — they are part of the real
        name.

        Examples:
          "Cholamandalam Investment & Finance Co. Ltd."  → "Cholamandalam Investment & Finance"
          "NMDC Limited"                                  → "NMDC"
          "HCL Technologies Ltd."                         → "HCL Technologies"
          "Indian Bank"                                   → "Indian Bank"  (no suffix found)
        """
        # Only strip true legal-entity suffixes — not generic business words.
        # The pattern anchors to 'end-of-string' after the suffix so that
        # 'Corp. of India Ltd.' only strips at 'Ltd.' (the final suffix),
        # not at 'Corp.' in the middle.
        # Strategy: apply repeatedly until stable (handles 'Pvt. Ltd.' chains).
        _SUFFIX_PAT = re.compile(
            r'\s*\b(?:'
            r'Private\s+Limited'
            r'|Pvt\.?\s+Ltd\.?'
            r'|Limited'
            r'|Ltd\.?'
            r'|Corporation'
            r'|Incorporated'
            r'|Inc\.?'
            r'|Co\.\s+Ltd\.?'
            r')\s*\.?\s*$',       # must be at END of string
            re.IGNORECASE
        )
        result = name
        while True:
            stripped = _SUFFIX_PAT.sub('', result).strip(' .,&-')
            if stripped == result:
                break
            result = stripped
        return result

    @staticmethod
    def _dedup_unknown_cns(contract_notes: List['ContractNote']) -> List['ContractNote']:
        """
        Remove CNs with cn_no='UNKNOWN' whose trades are already covered by
        CNs with real ECN numbers. This happens when both a 0096-format XLS
        (no CN column → 'UNKNOWN') and an NSDL STeADY XLS (proper ECN numbers)
        are uploaded for the same broker on the same date.
        Without de-duplication, C3 shows phantom NSDL_MISSING rows alongside
        the real MATCH rows for the same trade.
        """
        # Index real CNs by (ucc, isin, side) AND by (isin, side) alone.
        # UCC style differs between file types: PDF uses SEBI UCCs (INST21860)
        # while 0096/block-deals XLS uses WS MAPIN UCCs (GWPJ0004, MYSTICV).
        # The UCC-only index would miss cross-format duplicates, so we also
        # maintain a UCC-agnostic index: if ANY real CN covers (isin, side)
        # from the same broker, the UNKNOWN CN is redundant for C3.
        real_keys: set = set()       # (ucc, isin, side)
        real_isin_keys: set = set()  # (isin, side) — UCC-agnostic
        for cn in contract_notes:
            if cn.cn_no and cn.cn_no not in ('', 'UNKNOWN', 'CNM26'):
                for t in cn.trades:
                    real_keys.add((cn.ucc or '', t.isin, t.side.capitalize()))
                    real_isin_keys.add((t.isin, t.side.capitalize()))

        if not real_keys:
            return contract_notes  # nothing to de-dup against

        # Build STT index from UNKNOWN CNs so we can transfer values to real CNs.
        # 0096 XLS files carry the authoritative STT totals; PDF CNs have stt_total=0.
        # Key: (ucc_upper, isin, side) → stt_total
        unknown_stt: dict = {}
        for cn in contract_notes:
            if cn.cn_no == 'UNKNOWN':
                for t in cn.trades:
                    if t.stt_total and t.stt_total > 0:
                        unknown_stt[(cn.ucc or '', t.isin, t.side.capitalize())] = t.stt_total

        filtered = []
        for cn in contract_notes:
            if cn.cn_no != 'UNKNOWN':
                # Enrich real CN trades with STT from matching UNKNOWN CN if missing
                for t in cn.trades:
                    if not t.stt_total:
                        key = (cn.ucc or '', t.isin, t.side.capitalize())
                        stt = unknown_stt.get(key)
                        if stt:
                            t.stt_total = stt
                filtered.append(cn)
                continue
            # Keep UNKNOWN CN only if at least one trade is NOT covered by any real CN.
            # Check both UCC-exact and UCC-agnostic: if a real CN covers the same
            # (isin, side) from any UCC, this UNKNOWN trade is a duplicate in C3.
            has_uncovered = any(
                (cn.ucc or '', t.isin, t.side.capitalize()) not in real_keys
                and (t.isin, t.side.capitalize()) not in real_isin_keys
                for t in cn.trades
            )
            if has_uncovered:
                filtered.append(cn)
            else:
                logger.debug(
                    "C3: dropping UNKNOWN CN (all trades covered by real ECN CNs): "
                    "ucc=%s trades=%s", cn.ucc, [t.isin for t in cn.trades]
                )
        return filtered

    @staticmethod
    def _dedup_ecn_duplicates(contract_notes: List['ContractNote']) -> List['ContractNote']:
        """
        When the same ECN number appears more than once (e.g. from both a broker PDF
        and an NSDL STeADY XLS), keep only one copy.  Preference order:
          1. The copy whose UCC matches a known pool mapin (canonical source)
          2. The first one encountered if no preference can be determined
        This prevents quantity doubling in C2 when broker files overlap with NSDL STeADY.
        """
        from collections import defaultdict
        # Group by (cn_no, isin) — real ECN numbers are globally unique per ISIN
        groups: dict = defaultdict(list)
        for cn in contract_notes:
            if cn.cn_no and cn.cn_no != 'UNKNOWN':
                for t in cn.trades:
                    groups[(cn.cn_no, t.isin)].append(cn)
            # UNKNOWN CNs pass through (handled by _dedup_unknown_cns)

        # Determine which CNs are duplicates (same ECN+ISIN, multiple entries)
        to_remove: set = set()
        for (cn_no, isin), cns in groups.items():
            if len(cns) <= 1:
                continue
            # Prefer the CN that appeared first (deterministic)
            # All extras are duplicates from different sources
            keeper = cns[0]
            for dup in cns[1:]:
                if id(dup) != id(keeper):
                    to_remove.add(id(dup))

        if not to_remove:
            return contract_notes

        kept = [cn for cn in contract_notes if id(cn) not in to_remove]
        removed = len(contract_notes) - len(kept)
        if removed:
            import logging as _log
            _log.getLogger(__name__).info(
                "ECN dedup: removed %d duplicate CN(s) (same ECN from multiple sources)",
                removed
            )
        return kept

    def _check3_cn_vs_nsdl(self, contract_notes: List[ContractNote],
                            nsdl_records: List[NSDLRecord],
                            isin_to_name: Dict[str, str] = None) -> List[Check3Result]:
        """
        Compare every broker CN against NSDL records.

        Matching priority:
          1. ECN number (CN number) — primary key, same as before
          2. ISIN + security name truncated at "Ltd/Limited" — secondary fallback
             so that minor name variations ("NMDC Ltd." vs "NMDC Limited")
             still match. Names are compared after stripping the corporate-
             suffix portion, so "NMDC" == "NMDC".

        isin_to_name: optional {ISIN → full name} map from Z8/OrderLog used
                      to enrich CN trade records that have ISINs but blank names
                      (common with 0096-format XLS CNs).
        """
        if isin_to_name is None:
            isin_to_name = {}

        # If no NSDL records are available, skip C3 entirely rather than
        # marking every CN trade as NSDL_MISSING.
        if not nsdl_records:
            return []

        # UNKNOWN dedup already applied before C2; re-run to catch any remaining
        contract_notes = self._dedup_unknown_cns(list(contract_notes))
        contract_notes = self._dedup_ecn_duplicates(contract_notes)

        _SIDE_WORDS = {'buy', 'sell', 'b', 's', 'pur', 'purchase'}
        _ISIN_PAT   = re.compile(r'^[A-Z]{2}[A-Z0-9]{10}$')

        def _resolve_name(isin: str, name: str) -> str:
            """Return best available name — always prefers the security master.
            Treats as blank: Buy/Sell side keywords, raw ISIN strings (XLS CNs),
            and empty names. Falls back to isin_to_name from Z8_SecurityDetail."""
            clean = (name or '').strip()
            # Treat side keywords as blank (PDF captured Buy/Sell as name)
            if clean.lower() in _SIDE_WORDS:
                clean = ''
            # Treat raw ISIN string as blank (XLS CN has no name, uses ISIN as placeholder)
            if _ISIN_PAT.match(clean):
                clean = ''
            if clean:
                return clean
            return isin_to_name.get(isin, isin)

        results = []

        # ── Drop NSDL trades rejected by the fund manager ────────────────
        # A rejected trade is re-sent by the broker with an N-suffixed ECN
        # (e.g. 0008749 rejected → A990008749N corrected). Both legs land in
        # the NSDL file, so we exclude anything whose status contains
        # "rejected" and let the surviving (corrected) leg match normally.
        _filtered_nsdl = []
        for nr in nsdl_records:
            if 'rejected' in (nr.ecn_status or '').lower():
                logger.info(f'NSDL filter: dropping {nr.ecn_no.strip()} '
                            f'(status="{nr.ecn_status}")')
                continue
            _filtered_nsdl.append(nr)
        if len(_filtered_nsdl) != len(nsdl_records):
            logger.info(f'NSDL filter: {len(nsdl_records) - len(_filtered_nsdl)} '
                        f'rejected trade(s) removed')
        nsdl_records = _filtered_nsdl

        # ── Index NSDL by ECN No ──────────────────────────────────────────
        nsdl_by_ecn: Dict[str, NSDLRecord] = {}
        for nr in nsdl_records:
            ecn = nr.ecn_no.strip()
            nsdl_by_ecn[ecn] = nr
            # Strip clearing-member prefix: 'A990323/00778085' → '778085'
            bare = re.sub(r'^[A-Z]\d+/', '', ecn).lstrip('0') or ecn
            if bare and bare != ecn:
                nsdl_by_ecn[bare] = nr

        # ── Secondary index: (ISIN, trunc_name) → NSDLRecord ─────────────
        # Used when ECN matching fails (e.g. broker sends a different CN number
        # format than what NSDL stores, but the security + ISIN is unambiguous).
        # Build as list-of-records so that (isin, name) collisions
        # (e.g. two MPS trades with different qtys, same truncated name)
        # don't silently overwrite each other. Pass 2 only fires when unique.
        nsdl_by_isin_name: Dict[tuple, list] = {}
        for nr in nsdl_records:
            if nr.isin:
                # Strip exchange segment suffix (e.g. 'NRB BEARINGS LIMITED EQ NEW FV RS.2'
                # → keep only the company name part before EQ/BE/N1 etc.)
                _nsdl_raw = re.sub(r'\s+(?:EQ|BE|N1|N2|N3)(?:\s.*)?$', '',
                                   nr.security_name.strip(), flags=re.IGNORECASE)
                trunc = self._trunc_at_ltd(_nsdl_raw).upper()
                nsdl_by_isin_name.setdefault((nr.isin, trunc), []).append(nr)

        # ── Match broker CNs → NSDL ──────────────────────────────────────
        cn_set = set()
        matched_nsdl_ecns: set = set()   # track which NSDL ECNs were matched

        for cn in contract_notes:
            for trade in cn.trades:
                cn_set.add(cn.cn_no.strip())
                nsdl_key  = cn.cn_no.strip()

                # Pass 1: ECN number match
                # Strip clearing-member prefix from CN number before lookup:
                # Broker sends A23260320/416153, NSDL indexes A99260320/416153 and 416153.
                # Stripping ^[A-Z]\d+/ from both sides gives the bare numeric suffix.
                _cn_bare = re.sub(r'^[A-Z]\d+/', '', nsdl_key).lstrip('0') or nsdl_key
                nsdl_rec = (nsdl_by_ecn.get(nsdl_key) or      # exact
                            nsdl_by_ecn.get(_cn_bare)   or      # bare suffix: 416153
                            nsdl_by_ecn.get('A99' + nsdl_key))  # legacy A99 prefix

                # Pass 2: ISIN + truncated-name match (fallback)
                # Resolve name using security master — handles cases where PDF
                # captured 'Buy'/'Sell' instead of the security name.
                # Only fires when exactly ONE unmatched candidate shares this name,
                # preventing cross-pool confusion (e.g. two MPS trades, different qtys).
                if not nsdl_rec and trade.isin:
                    cn_name   = _resolve_name(trade.isin, trade.security_name)
                    trunc_cn  = self._trunc_at_ltd(cn_name).upper()
                    _p2_cands = [nr for nr in nsdl_by_isin_name.get((trade.isin, trunc_cn), [])
                                 if nr.ecn_no.strip() not in matched_nsdl_ecns]
                    if len(_p2_cands) == 1:
                        nsdl_rec = _p2_cands[0]

                # Pass 3: ISIN-only match — last resort when names differ too much
                # (e.g. 'MAZAGON DOCK SHIPBUIL' vs 'MAZAGON DOCK SHIPBUILDERS').
                # Only used when exactly ONE NSDL record exists for this ISIN.
                if not nsdl_rec and trade.isin:
                    _isin_recs = [nr for nr in nsdl_records if nr.isin == trade.isin
                                  and nr.ecn_no.strip() not in matched_nsdl_ecns]
                    if len(_isin_recs) == 1:
                        nsdl_rec = _isin_recs[0]
                        logger.debug(
                            'C3 isin-only match: CN %s isin=%s → NSDL ECN %s',
                            cn.cn_no, trade.isin, nsdl_rec.ecn_no
                        )

                # Pass 4: ISIN + qty match — for UNKNOWN CNs (XLS-sourced, no ECN)
                # when multiple NSDL records exist for the same ISIN but each has
                # a unique quantity (e.g. WEVA KOTAK 50 vs MYSTICV 100 for Bosch).
                if not nsdl_rec and trade.isin and trade.qty:
                    _qty = round(float(trade.qty))
                    _qty_recs = [nr for nr in nsdl_records
                                 if nr.isin == trade.isin
                                 and round(float(nr.qty or 0)) == _qty
                                 and nr.ecn_no.strip() not in matched_nsdl_ecns]
                    if len(_qty_recs) == 1:
                        nsdl_rec = _qty_recs[0]
                        logger.debug(
                            'C3 isin+qty match: CN %s isin=%s qty=%s → NSDL ECN %s',
                            cn.cn_no, trade.isin, _qty, nsdl_rec.ecn_no
                        )
                    if nsdl_rec:
                        logger.debug(
                            "C3 name-match: CN %s isin=%s name-trunc=%r → NSDL ECN %s",
                            cn.cn_no, trade.isin, trunc_cn, nsdl_rec.ecn_no
                        )

                if nsdl_rec:
                    status      = 'MATCH'
                    nsdl_status = nsdl_rec.ecn_status
                    _nsdl_ecn = nsdl_rec.ecn_no.strip()
                    matched_nsdl_ecns.add(_nsdl_ecn)
                    # Mark all variants so the NSDL-only loop doesn't
                    # re-emit a PDF_MISSING row for the same trade
                    _nsdl_bare = re.sub(r'^[A-Z]\d+/', '', _nsdl_ecn).lstrip('0')
                    if _nsdl_bare:
                        matched_nsdl_ecns.add(_nsdl_bare)
                    # Also mark the CN number without leading zeros
                    _cn_num = cn.cn_no.strip().lstrip('0')
                    if _cn_num:
                        matched_nsdl_ecns.add(_cn_num)
                else:
                    status      = 'NSDL_MISSING'
                    nsdl_status = ''

                display_name = _resolve_name(trade.isin, trade.security_name)

                results.append(Check3Result(
                    cn_no=cn.cn_no,
                    isin=trade.isin,
                    security_name=display_name,
                    broker_sebi=cn.broker_sebi,
                    broker_name=cn.broker_name,
                    ucc=cn.ucc,
                    qty=trade.qty,
                    net_rate=trade.wap,
                    in_broker_pdf=True,
                    in_nsdl=(status == 'MATCH'),
                    nsdl_status=nsdl_status,
                    status=status,
                ))

        # ── NSDL records with no matching broker CN ───────────────────────
        for nr in nsdl_records:
            _ecn_stripped = nr.ecn_no.strip()
            # Strip A99 prefix and trailing letter suffix (e.g. A990008749N → 0008749 → 8749)
            bare = re.sub(r'^A\d+', '', _ecn_stripped).rstrip('ABCDEFGHIJKLMNOPQRSTUVWXYZ')
            bare_num = bare.lstrip('0') or bare
            already_matched = (
                _ecn_stripped in matched_nsdl_ecns or
                bare          in matched_nsdl_ecns or
                bare_num      in matched_nsdl_ecns or
                bare in cn_set or
                bare_num in cn_set or
                _ecn_stripped in cn_set
            )
            if already_matched:
                continue
            # Use security master name if available; fall back to NSDL name
            _nr_display = (isin_to_name.get(nr.isin or '') or
                           nr.security_name or nr.isin or '')
            results.append(Check3Result(
                cn_no=nr.ecn_no,
                isin=nr.isin,
                security_name=_nr_display,
                broker_sebi=nr.broker_sebi,
                broker_name=nr.broker_name,
                ucc=nr.ucc,
                qty=nr.qty,
                net_rate=nr.net_rate,
                in_broker_pdf=False,
                in_nsdl=True,
                nsdl_status=nr.ecn_status,
                status='PDF_MISSING',
            ))

        return results

    # ── Check 4 — Generate 0096 ───────────────────────────────────────────── #

    def _generate_0096(self, contract_notes: List[ContractNote],
                        dealer_trades: List[DealerTrade],
                        isin_to_instr: Dict[str, str],
                        date_str: str,
                        isin_to_nse_ticker: Dict[str, str] = None) -> List[Output0096Row]:
        """
        Generate WS 0096 upload rows.
        One row per contract note × Mapin (= one CN per pool per broker per ISIN).
        Price and qty come from the broker PDF (authoritative).
        Charges calculated using broker-specific rates from broker_map.

        0096 SCRIPT CODE column uses NSE ticker symbols (e.g. HCLTECH) from the
        Z8 security master when available, falling back to WS instrument codes
        (e.g. EQ26121) from isin_to_instr.

        STT column is total STT in ₹ per row (not per share).
        Brokerage is stored to 5 decimal places.
        """
        if isin_to_nse_ticker is None:
            isin_to_nse_ticker = {}
        rows = []

        for cn in contract_notes:
            # Look up broker details — try short sebi_code, then full reg number, then name
            broker = (self._brokers_by_sebi.get(cn.broker_sebi.upper()) or
                      self._brokers_by_reg_no.get(cn.broker_sebi.upper()) or
                      self._find_broker_by_name(cn.broker_name))
            # 0096 Column A = dealer_code (the broker's internal trading code, e.g. EDEL)
            # NOT the SEBI registration number
            dealer_code   = broker.get('dealer_code', '') if broker else ''
            brok_rate     = broker.get('brokerage_rate', 0.001) if broker else 0.001

            # Determine settlement date — use CN date, else T+1
            settle_date = cn.settlement_date
            if not settle_date:
                settle_date = _add_working_days(cn.trade_date, 1)

            for trade in cn.trades:
                # SCRIPT CODE: the 0096 SecurityCode column MUST be the
                # NSEMAPPING value from Z8_SecurityDetail (column L), looked up
                # by the ISIN printed on the contract note. WS's Java mapper
                # rejects (NullPointerException) files where this column carries
                # the WS internal instrument code (EQxxxxxx) — that's a Z13
                # INSTRUMENT_CODE and is NOT what the upload expects.
                script_code = isin_to_nse_ticker.get(trade.isin, '')
                if not script_code:
                    logger.warning(
                        '0096: NSEMAPPING not found in Z8 for ISIN %s (CN %s '
                        'broker %s) — SecurityCode will be blank and WS will '
                        'reject this row',
                        trade.isin, cn.cn_no, cn.broker_sebi,
                    )

                charges = calculate_charges(
                    trade.qty, trade.wap, brok_rate, trade.side
                )
                # Brokerage: use CN value if present, else calculate at 5dp
                brok_per_share = round(
                    trade.brokerage_per_share or charges['brokerage_per_share'], 5
                )
                # STT: use total as parsed directly from the CN
                stt_total = int(trade.stt_total) if trade.stt_total else 0

                # MAPIN ID: use canonical pool mapin (not CN UCC which may be a
                # broker back-office code like 25281 or HDFC00001816).
                # Resolve: cn.ucc → pool_map → canonical_mapin → pool mapin
                _pool = self._mapin_to_pool.get((cn.ucc or '').upper(), {})
                _mapin = (_pool.get('canonical_mapin') or
                          _pool.get('mapin') or
                          cn.ucc)

                # Cash settlement date:
                # BUY → trade date (same as Transaction Date)
                # SELL → settlement date (T+1)
                _is_buy = trade.side.lower() == 'buy'
                _cash_sett = cn.trade_date if _is_buy else settle_date

                rows.append(Output0096Row(
                    broker_code           = dealer_code,
                    dummy                 = 0,
                    security_code         = script_code,
                    exchange              = trade.exchange,
                    transaction_type      = 'BY-' if _is_buy else 'SL+',
                    transaction_date      = cn.trade_date,
                    settlement_date       = settle_date,
                    quantity              = trade.qty,
                    price                 = trade.wap,
                    brokerage_per_share   = brok_per_share,
                    service_tax           = 0.0,
                    settlement_flag       = 'F',
                    market_rate           = 0.0,
                    cash_symbol           = 'CASH',
                    block_flag            = 'Y',
                    stt                   = stt_total,
                    accrued_interest      = 0.0,
                    mapin_id              = _mapin,
                    remarks               = '',          # blank per required format
                    cash_settlement_date  = _cash_sett,
                    stamp_duty            = 0.0,
                ))

        return rows

    def _find_broker_by_name(self, name: str) -> Optional[dict]:
        if not name:
            return None
        name_up = name.upper()
        for b in self._brokers_by_dealer.values():
            if b['name'].upper() in name_up or name_up in b['name'].upper():
                return b
        return None

    # ── Check 5 ───────────────────────────────────────────────────────────── #

    def _check5_exchange(self, exchange_trades: List[ExchangeTrade],  # noqa: C901
                          contract_notes: List['ContractNote'],
                          isin_to_instr: Dict[str, str] = None,
                          sec_name_to_isin: Dict[str, str] = None) -> List[Check5Result]:
        """
        Compare exchange trades to contract note totals by ISIN × side.

        Exchange PDFs carry company names only ("NMDC Limited", "Seamec Limited").
        Resolution pipeline (applied to every exchange trade before aggregation):
          1. Embedded ISIN in the security field (NSE format "INE121A01024 CIFC")
          2. Exact uppercase match in sec_name_to_isin (catches NSE/BSE tickers)
          3. Normalised full-name match (Ltd/Limited, & → AND, punctuation stripped)
          4. Reverse isin_to_instr scan (instrument code → ISIN)
          5. Word-overlap fuzzy match (≥2 meaningful words; last resort)

        Bugs fixed vs previous version
        --------------------------------
        * Removed three dead implementations that followed the first return.
        * Resolution fallback: when _resolve() fails, exch_agg keys on the raw
          security name.  The dealer loop now retries _resolve(raw_key) on the fly
          so unresolved entries are still matched rather than marked DEALER_ONLY.
        * EXCHANGE_ONLY dedup: the clean-up loop now resolves ekey → ISIN before
          the "already in results" check, preventing duplicate rows.
        * Added warning log listing every security name that could not be resolved.
        """
        import re as _re
        results: List[Check5Result] = []
        if isin_to_instr    is None: isin_to_instr    = {}
        if sec_name_to_isin is None: sec_name_to_isin = {}

        # Words too common to be useful in a fuzzy overlap match
        _STOP = {
            'LIMITED', 'LTD', 'PRIVATE', 'PVT', 'COMPANY', 'CO',
            'INDIA', 'INDUSTRIES', 'CORP',
            'THE', 'OF', 'AND', 'FINANCE', 'FINANCIAL',
            'INVESTMENT', 'SERVICES', 'SECURITIES',
            # Note: BANK, CORPORATION removed — they are distinctive enough
            # (Karnataka Bank ≠ ambiguous; Transport Corporation ≠ ambiguous)
        }

        def _norm(s: str) -> str:
            """Normalize a company name: uppercase, strip punctuation, standardize abbreviations."""
            n = str(s).upper().strip()
            n = _re.sub(r"'",          '',    n)   # Divi's → Divis
            n = _re.sub(r'&',          'AND', n)
            n = _re.sub(r'\bLIMITED\b', 'LTD', n)
            n = _re.sub(r'\bLTD\.?\b', 'LTD', n)
            n = _re.sub(r'\bPRIVATE\b', 'PVT', n)
            n = _re.sub(r'\bPVT\.?\b', 'PVT', n)
            n = _re.sub(r'\bCOMPANY\b', 'CO',  n)
            n = _re.sub(r'\bCO\.?\b',  'CO',  n)
            n = _re.sub(r'[,.\-]',    ' ',   n)   # strip punctuation
            n = _re.sub(r'\s+',       ' ',   n).strip()
            return n

        # Set of ISINs from contract notes — used to validate exchange ISINs.
        # Exchange PDFs sometimes carry a non-standard ISIN (e.g. IN9036D01059
        # for KVB partly-paid shares) that doesn't match the CN's canonical
        # ISIN (INE036D01028).  We only trust an exchange-supplied ISIN when it
        # is actually one the CNs traded; otherwise we fall through to
        # name-based resolution.
        _cn_isins = {t.isin for cn in contract_notes for t in cn.trades if t.isin}

        def _resolve(name: str, isin: str = '') -> str:
            """Resolve exchange security name/code to an ISIN.

            Priority:
              1. Embedded ISIN — but only if it's a known dealer ISIN.
                 If the exchange supplies a non-standard ISIN (e.g. partly-paid
                 shares class) that the dealer did not trade, ignore it and
                 fall through to name-based resolution.
              2. Exact uppercase key in sec_name_to_isin (NSE/BSE tickers).
              3. Normalised full-name key in sec_name_to_isin.
              4. Reverse scan of isin_to_instr (instrument code → ISIN).
              5. Word-overlap fuzzy match against sec_name_to_isin keys (≥2 words).
            """
            if isin and isin in _cn_isins:
                return isin   # known dealer ISIN — use directly

            upper = name.upper().strip()

            # 1. Exact match — catches tickers like CIFC, INBK, NMDC
            if upper in sec_name_to_isin:
                return sec_name_to_isin[upper]

            # 2. Normalised name match
            norm = _norm(name)
            if norm in sec_name_to_isin:
                return sec_name_to_isin[norm]

            # 3. Reverse isin_to_instr (instrument code → ISIN)
            for isin_v, instr in isin_to_instr.items():
                if instr and instr.upper() == upper:
                    return isin_v

            # 4. Word-overlap fuzzy (last resort; requires ≥2 meaningful matching words)
            key_words = {w for w in norm.split() if w not in _STOP and len(w) > 2}
            if len(key_words) >= 2:
                best_isin, best_score = '', 0
                for sec_key, sec_isin in sec_name_to_isin.items():
                    if len(sec_key) <= 6:          # skip short ticker keys
                        continue
                    sec_words = {w for w in sec_key.split() if w not in _STOP and len(w) > 2}
                    overlap = len(key_words & sec_words)
                    if overlap > best_score:
                        best_isin, best_score = sec_isin, overlap
                if best_score >= 2:
                    return best_isin

            return ''

        # ── CN aggregation (keyed on ISIN × side) ────────────────────────────
        cn_agg: Dict[Tuple, dict] = defaultdict(
            lambda: {'qty': 0.0, 'value': 0.0, 'security': '', 'name': ''}
        )
        for cn in contract_notes:
            for t in cn.trades:
                key = (t.isin, t.side.capitalize())
                cn_agg[key]['qty']      += t.qty
                cn_agg[key]['value']    += t.qty * t.wap   # always use wap×qty; total_value can be brokerage-adjusted
                cn_agg[key]['security']  = t.security_name or cn_agg[key]['security']
                cn_agg[key]['name']      = t.security_name or cn_agg[key]['name']

        # ── Exchange aggregation: resolve each trade to ISIN before keying ─────
        exchange_name = exchange_trades[0].exchange if exchange_trades else 'EXCHANGE'
        exch_agg: Dict[Tuple, dict] = defaultdict(
            lambda: {'qty': 0.0, 'value': 0.0, 'security': '', 'isin': '', 'raw_key': ''}
        )
        unresolved: list = []
        for et in exchange_trades:
            resolved_isin = _resolve(et.security, et.isin)
            if not resolved_isin:
                unresolved.append(et.security)
            key = (resolved_isin or et.security, et.side)
            exch_agg[key]['qty']      += et.qty
            exch_agg[key]['value']    += et.trade_value or (et.qty * et.price)
            exch_agg[key]['security']  = et.security
            exch_agg[key]['isin']      = resolved_isin
            # Keep the original name so fallback _resolve() can retry it later
            exch_agg[key]['raw_key']   = et.security

        if unresolved:
            logger.warning(
                "C5 %s: %d exchange trade(s) could not be resolved to ISIN via security master. "
                "Names: %s",
                exchange_name, len(unresolved),
                sorted(set(unresolved)),
            )
        logger.debug(
            "C5 %s: exch_agg keys=%s  cn_agg keys=%s",
            exchange_name,
            sorted(str(k) for k in exch_agg),
            sorted(str(k) for k in cn_agg),
        )

        # ── Match CN ISINs against exchange entries ────────────────────────────
        processed_exch: set = set()

        for (isin, side), d in sorted(cn_agg.items()):
            d_qty = d['qty']
            d_px  = round(d['value'] / d_qty, 4) if d_qty > 0 else 0.0
            d_sec = d['security']

            # Pass 1: direct key lookup (works when resolution succeeded)
            e = exch_agg.get((isin, side)) or {}
            if e:
                processed_exch.add((isin, side))
            else:
                # Pass 2: scan exch_agg for same ISIN, same side — handles
                # unresolved names that keyed on the raw security string.
                for (ekey, eside), ev in exch_agg.items():
                    if eside != side:
                        continue
                    ev_isin = ev.get('isin', '')
                    raw     = ev.get('raw_key', ekey)
                    if ev_isin == isin or _resolve(raw) == isin:
                        e = ev
                        ev['isin'] = isin
                        processed_exch.add((ekey, eside))
                        if not ev_isin:
                            logger.info(
                                "C5 %s: late-resolved '%s' → %s (side=%s) via fallback scan",
                                exchange_name, raw, isin, side,
                            )
                        break

            # Pass 3: opposite-side fallback for CID-encoded PDFs.
            # Exchange PDFs with CID font encoding garble the Buy/Sell column,
            # causing all trades to default to 'Buy'.  When a dealer Sell (or Buy)
            # has no same-side exchange entry, check the opposite side with matching
            # ISIN and qty.  A qty match within 1% is treated as SIDE_UNVERIFIED
            # rather than a real break — the economic substance matches.
            _cid_fallback = False
            if not e:
                opp_side = 'Sell' if side == 'Buy' else 'Buy'
                for (ekey, eside), ev in exch_agg.items():
                    if eside != opp_side:
                        continue
                    ev_isin = ev.get('isin', '')
                    raw     = ev.get('raw_key', ekey)
                    if ev_isin != isin and _resolve(raw) != isin:
                        continue
                    # Qty must match within 1% — otherwise it's a real discrepancy
                    e_qty_cand = ev.get('qty', 0.0)
                    if d_qty > 0 and abs(e_qty_cand - d_qty) / d_qty <= 0.01:
                        e = ev
                        ev['isin'] = isin
                        processed_exch.add((ekey, eside))
                        _cid_fallback = True
                        logger.warning(
                            "C5 %s: ISIN %s matched via OPPOSITE SIDE (%s dealer → %s exchange). "
                            "Exchange PDF may have CID-encoded side column — verify manually.",
                            exchange_name, isin, side, opp_side,
                        )
                        break

            # Pass 4: alternate-ISIN fallback.
            # Exchange PDFs sometimes carry a different ISIN for the same security
            # (e.g. IN9036D01059 vs INE036D01028 for KVB — partly-paid vs EQ shares).
            # When all ISIN-based passes fail, compare full company names.
            # Use the CN security name for word overlap.
            if not e:
                # Prefer the full company name for word overlap
                _d_full_name = cn_agg[(isin, side)].get('name', '') or d_sec
                d_name_norm = _re.sub(r'[^A-Z0-9 ]', '', _d_full_name.upper())
                for (ekey, eside), ev in exch_agg.items():
                    if (ekey, eside) in processed_exch:
                        continue
                    if eside != side:
                        continue
                    e_sec_norm = _re.sub(r'[^A-Z0-9 ]', '',
                                         ev.get('security', ekey).upper())
                    # Require ≥2 meaningful words overlap
                    d_words = {w for w in d_name_norm.split() if w not in _STOP and len(w) > 2}
                    e_words = {w for w in e_sec_norm.split() if w not in _STOP and len(w) > 2}
                    # Require >=2 overlapping words, OR >=1 if the shorter
                    # name only has 1 meaningful word (e.g. 'KARNATAKA BANK'
                    # where BANK is stripped → only 'KARNATAKA' remains).
                    _min_words = min(len(d_words), len(e_words))
                    _threshold = max(1, min(2, _min_words))
                    if len(d_words & e_words) >= _threshold and len(d_words & e_words) >= 1:
                        e_qty_cand = ev.get('qty', 0.0)
                        if d_qty > 0 and abs(e_qty_cand - d_qty) / d_qty <= 0.01:
                            e = ev
                            ev['isin'] = isin   # normalise to dealer ISIN
                            processed_exch.add((ekey, eside))
                            logger.info(
                                "C5 %s: ISIN mismatch resolved by name — dealer %s → exchange %s "
                                "(both match '%s').",
                                exchange_name, isin, ev.get('isin', ekey), d_sec,
                            )
                            break

            e_qty = e.get('qty', 0.0)
            e_px  = round(e.get('value', 0.0) / e_qty, 4) if e_qty > 0 else 0.0
            e_sec = e.get('security', '') or d_sec

            qty_diff   = round(d_qty - e_qty, 4)
            price_diff = round(d_px  - e_px,  4)

            if   e_qty == 0:                                               status = 'CN_ONLY'
            elif d_qty == 0:                                               status = 'EXCHANGE_ONLY'
            elif _cid_fallback and abs(qty_diff) <= 0.01 and abs(price_diff) <= PRICE_TOLERANCE:
                                                                           status = 'MATCH'   # CID side garble but qty/px agree — economic match
            elif _cid_fallback:                                            status = 'SIDE_UNVERIFIED'
            elif abs(qty_diff) > 0.01 and abs(price_diff) > PRICE_TOLERANCE: status = 'QTY_AND_PRICE_BREAK'
            elif abs(qty_diff) > 0.01:                                    status = 'QTY_BREAK'
            elif abs(price_diff) > PRICE_TOLERANCE:                       status = 'PRICE_BREAK'
            else:                                                          status = 'MATCH'

            results.append(Check5Result(
                security=e_sec or d_sec, isin=isin, side=side,
                exchange=exchange_name,
                exchange_qty=e_qty, exchange_avg_px=e_px,
                cn_qty=d_qty,  cn_avg_px=d_px,
                qty_diff=qty_diff, price_diff=price_diff, status=status,
            ))

        # ── Exchange-only entries (no matching dealer trade) ───────────────────
        for (ekey, eside), e in exch_agg.items():
            if (ekey, eside) in processed_exch:
                continue
            # Resolve the key to an ISIN for the dedup check.
            # Bug fix: previously used (e['isin'] or ekey) which is a raw security
            # name when isin='', causing any(r.isin == security_name) to always be
            # False and emitting a spurious duplicate EXCHANGE_ONLY row.
            e_isin = e.get('isin', '') or _resolve(ekey)
            if e_isin and any(r.isin == e_isin and r.side == eside for r in results):
                continue   # already covered by a dealer-side row above
            e_qty = e.get('qty', 0.0)
            e_px  = round(e.get('value', 0.0) / e_qty, 4) if e_qty > 0 else 0.0
            results.append(Check5Result(
                security=e.get('security', ekey), isin=e_isin or ekey, side=eside,
                exchange=exchange_name,
                exchange_qty=e_qty, exchange_avg_px=e_px,
                cn_qty=0.0, cn_avg_px=0.0,
                qty_diff=-e_qty, price_diff=0.0, status='EXCHANGE_ONLY',
            ))

        return results


# ── Excel report writer ───────────────────────────────────────────────────── #

def write_0096_excel(rows: List[Output0096Row], out_path: str, date_str: str):
    """Write the WS 0096 upload file as Excel 97-2003 (.xls) — required upload format."""
    import xlwt
    wb = xlwt.Workbook(encoding='utf-8')
    ws = wb.add_sheet('SHEET')

    # Header names must match WS's Java mapper exactly, which expects NO
    # spaces. The Upload_Formats/0096 Block Deals.xls Sample sheet carries
    # spaced labels ("Broker Code"), but a file actually accepted by WS on
    # 2026-04-13 (0096 Block Deals 13042026.xls) uses no-space headers
    # ("BrokerCode"). The Sample sheet is a human-readable cheat sheet, not
    # the mapper's authoritative header list.
    headers = [
        'BrokerCode', 'Dummy', 'SecurityCode', 'Exchange',
        'TransactionType', 'TransactionDate', 'SettlementDate',
        'Quantity', 'Price', 'BrokeragePerShare', 'ServiceTaxPerShare',
        'SettlementFlag', 'MarketRate', 'CashSymbolcode', 'BlockFlag',
        'SecurityTransactionTax', 'AccruedInterestPerUnit',
        'MapinID', 'Renarks', 'CashsettlementDate', 'StampDuty',
    ]

    # Header style — dark blue with white bold text
    hdr_style = xlwt.easyxf(
        'font: bold true, colour white, height 200;'
        'pattern: pattern solid, fore_colour dark_blue;'
        'alignment: horizontal centre;'
        'borders: left thin, right thin, top thin, bottom thin;'
    )
    # Data styles
    def _style(num_fmt=''):
        s = 'borders: left thin, right thin, top thin, bottom thin;'
        if num_fmt:
            return xlwt.easyxf(s, num_format_str=num_fmt)
        return xlwt.easyxf(s)

    # Dates must be written as Excel serial integers, not strings.
    # WS rejects string dates in the 0096 upload file.
    # Excel serial = days since 30-Dec-1899.
    from datetime import datetime as _ddt
    _EPOCH = _ddt(1899, 12, 30)
    def _serial(date_str: str):
        """Return Excel serial int for a DD/MM/YYYY date, or '' if blank/invalid."""
        if not date_str or not str(date_str).strip():
            return ''
        for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y'):
            try:
                return (_ddt.strptime(date_str.strip(), fmt) - _EPOCH).days
            except (ValueError, AttributeError):
                pass
        return ''

    style_default  = _style()
    style_4dp      = _style('#,##0.0000')    # brokerage — 4dp
    style_stt      = _style('#,##0')         # STT — integer
    style_date     = _style('DD/MM/YYYY')    # serial int + date format = displays as date
    style_price    = _style('#,##0.0000')

    for c, h in enumerate(headers):
        ws.write(0, c, h, hdr_style)
        ws.col(c).width = 4000  # default width

    # Wider columns
    for c, w in [(0,5600),(2,4200),(5,5000),(6,5000),(9,5600),(15,6700),(17,4500),(19,5000)]:
        ws.col(c).width = w

    for r_idx, row in enumerate(rows, 1):
        # Dummy (col B) must be written as an empty cell (xlrd type 0), NOT
        # numeric 0. Confirmed against a WS-accepted file (0096 Block Deals
        # 13042026.xls). Service Tax Per Share (K) stays numeric 0 and
        # StampDuty (U) stays blank, both matching that same file.
        vals = [
            row.broker_code, '', row.security_code, row.exchange,
            row.transaction_type,
            _serial(row.transaction_date),     # col 5  — serial date
            _serial(row.settlement_date),      # col 6  — serial date (blank if not set)
            row.quantity, row.price, row.brokerage_per_share, 0,
            row.settlement_flag, row.market_rate, row.cash_symbol, row.block_flag,
            row.stt, row.accrued_interest, row.mapin_id, row.remarks,
            _serial(row.cash_settlement_date), # col 19 — serial date
            '',   # StampDuty: blank
        ]
        styles = [
            style_default, style_default, style_default, style_default,
            style_default,
            style_date if vals[5] != '' else style_default,   # transaction_date
            style_date if vals[6] != '' else style_default,   # settlement_date
            style_price,   style_price,   style_4dp,     style_default,
            style_default, style_default, style_default, style_default,
            style_stt,     style_default, style_default, style_default,
            style_date if vals[19] != '' else style_default,  # cash_settlement_date
            style_default,
        ]
        for c, (v, st) in enumerate(zip(vals, styles)):
            ws.write(r_idx, c, v, st)

    # Freeze first row
    ws.set_panes_frozen(True)
    ws.set_horz_split_pos(1)

    wb.save(out_path)
    logger.info(f"0096 file written: {out_path} ({len(rows)} rows)")


def write_0096_xlsx(rows: List[Output0096Row], out_path: str, date_str: str):
    """Write the WS 0096 upload file as Office Open XML (.xlsx).

    Background: xlwt produces a minimal BIFF8 stream that Apache POI (the
    library behind WS's Java mapper) rejects with NullPointerException —
    several optional records POI expects are missing. Every WS-accepted
    file in our history was Excel-saved, never an xlwt direct output.

    openpyxl produces a fully POI-compatible OOXML file. POI's
    WorkbookFactory auto-detects xls vs xlsx by magic bytes, so the WS
    mapper handles xlsx natively without any server-side changes.

    Column layout, cell types (string/number/date), headers, and data
    semantics are identical to write_0096_excel — only the container
    format changes.
    """
    from openpyxl import Workbook as _XWB
    from openpyxl.styles import Font as _XFont, PatternFill as _XFill, \
        Alignment as _XAlign, Border as _XBorder, Side as _XSide
    from datetime import datetime as _ddt

    wb = _XWB()
    # Remove the default 'Sheet' and create one named 'SHEET' to match the
    # working Apr 13 file's sheet name. Assigning ws.title = 'SHEET' on the
    # default sheet triggers openpyxl's _unique_name which appends '1'
    # because 'Sheet' and 'SHEET' are case-equivalent.
    wb.remove(wb.active)
    ws = wb.create_sheet('SHEET')

    headers = [
        'BrokerCode', 'Dummy', 'SecurityCode', 'Exchange',
        'TransactionType', 'TransactionDate', 'SettlementDate',
        'Quantity', 'Price', 'BrokeragePerShare', 'ServiceTaxPerShare',
        'SettlementFlag', 'MarketRate', 'CashSymbolcode', 'BlockFlag',
        'SecurityTransactionTax', 'AccruedInterestPerUnit',
        'MapinID', 'Renarks', 'CashsettlementDate', 'StampDuty',
    ]

    hdr_font = _XFont(bold=True, color='FFFFFF', name='Calibri', size=10)
    hdr_fill = _XFill('solid', fgColor='1B2A4A')
    hdr_align = _XAlign(horizontal='center')
    thin = _XSide(style='thin', color='D0D5DE')
    hdr_border = _XBorder(left=thin, right=thin, top=thin, bottom=thin)

    for c, h in enumerate(headers, 1):
        cell = ws.cell(1, c, h)
        cell.font = hdr_font
        cell.fill = hdr_fill
        cell.alignment = hdr_align
        cell.border = hdr_border

    def _parse_date(date_str):
        if not date_str or not str(date_str).strip():
            return None
        for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y'):
            try:
                return _ddt.strptime(str(date_str).strip(), fmt)
            except (ValueError, AttributeError):
                continue
        return None

    for r_idx, row in enumerate(rows, 2):
        trans_date = _parse_date(row.transaction_date)
        settle_date = _parse_date(row.settlement_date)
        cash_settle = _parse_date(row.cash_settlement_date)

        ws.cell(r_idx, 1,  row.broker_code)                 # A BrokerCode
        ws.cell(r_idx, 2,  None)                            # B Dummy
        ws.cell(r_idx, 3,  row.security_code)               # C SecurityCode
        ws.cell(r_idx, 4,  row.exchange)                    # D Exchange
        ws.cell(r_idx, 5,  row.transaction_type)            # E TransactionType
        ws.cell(r_idx, 6,  trans_date)                      # F TransactionDate
        ws.cell(r_idx, 7,  settle_date)                     # G SettlementDate
        ws.cell(r_idx, 8,  row.quantity)                    # H Quantity
        ws.cell(r_idx, 9,  row.price)                       # I Price
        ws.cell(r_idx, 10, row.brokerage_per_share)         # J BrokeragePerShare
        ws.cell(r_idx, 11, 0)                               # K ServiceTaxPerShare
        ws.cell(r_idx, 12, row.settlement_flag)             # L SettlementFlag
        ws.cell(r_idx, 13, row.market_rate)                 # M MarketRate
        ws.cell(r_idx, 14, row.cash_symbol)                 # N CashSymbolcode
        ws.cell(r_idx, 15, row.block_flag)                  # O BlockFlag
        ws.cell(r_idx, 16, row.stt)                         # P SecurityTransactionTax
        ws.cell(r_idx, 17, row.accrued_interest)            # Q AccruedInterestPerUnit
        ws.cell(r_idx, 18, row.mapin_id)                    # R MapinID
        ws.cell(r_idx, 19, row.remarks or None)             # S Renarks
        ws.cell(r_idx, 20, cash_settle)                     # T CashsettlementDate
        ws.cell(r_idx, 21, None)                            # U StampDuty

        for col in (6, 7, 20):
            c = ws.cell(r_idx, col)
            if c.value is not None:
                c.number_format = 'DD/MM/YYYY'

    ws.freeze_panes = 'A2'
    wb.save(out_path)
    logger.info(f"0096 xlsx file written: {out_path} ({len(rows)} rows)")


def write_trade_recon_report(summary: TradeReconSummary, out_path: str):
    """Write the full trade reconciliation report as Excel (multiple sheets)."""
    wb = openpyxl.Workbook()

    NAVY  = PatternFill('solid', fgColor='1B2A4A')
    GREEN = PatternFill('solid', fgColor='EAF7F0')
    RED   = PatternFill('solid', fgColor='FDF1F1')
    AMBER = PatternFill('solid', fgColor='FDF5EB')
    ALT   = PatternFill('solid', fgColor='F5F7FA')
    WHT   = Font(bold=True, color='FFFFFF', name='Calibri', size=10)
    BORD  = Border(
        left=Side(style='thin', color='D0D5DE'),
        right=Side(style='thin', color='D0D5DE'),
        top=Side(style='thin', color='D0D5DE'),
        bottom=Side(style='thin', color='D0D5DE'),
    )

    def _hdr(ws, headers, col_widths=None):
        for c, h in enumerate(headers, 1):
            cell = ws.cell(1, c, h)
            cell.fill = NAVY; cell.font = WHT; cell.border = BORD
            cell.alignment = Alignment(horizontal='center')
        if col_widths:
            for i, w in enumerate(col_widths, 1):
                ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = 'A2'

    def _row_fill(status):
        if status == 'MATCH': return GREEN
        if status == 'UNFILLED': return AMBER  # unfilled order — not a break
        if status in ('OVERFILL', 'PRICE_BREACH', 'CN_MISSING', 'NSDL_MISSING',
                      'PDF_MISSING', 'QTY_AND_PRICE_BREAK'): return RED
        return AMBER

    def _write_rows(ws, rows_data):
        for r_idx, (vals, status) in enumerate(rows_data, 2):
            fill = _row_fill(status)
            for c, v in enumerate(vals, 1):
                cell = ws.cell(r_idx, c, v)
                cell.fill = fill; cell.border = BORD
                if isinstance(v, float):
                    cell.number_format = '#,##0.0000'

    # ── Summary sheet ─────────────────────────────────────────────────────
    ws_sum = wb.active; ws_sum.title = 'Summary'
    ws_sum.merge_cells('A1:F1')
    from core.date_format import display_date as _disp_d
    ws_sum['A1'] = f'Trade Reconciliation Report — {_disp_d(summary.date)}'
    ws_sum['A1'].font = Font(bold=True, size=14, color='1B2A4A')
    ws_sum['A1'].alignment = Alignment(horizontal='center', vertical='center')
    ws_sum.row_dimensions[1].height = 28

    summary_data = [
        ('Check 1: WS vs Dealer',    len(summary.check1_results), summary.c1_breaks),
        ('Check 2: Dealer vs CN',     len(summary.check2_results), summary.c2_breaks),
        ('Check 3: CN vs NSDL',       len(summary.check3_results), summary.c3_breaks),
        ('0096 Output Rows',          len(summary.output_0096),    0),
        ('Check 5: Exchange (opt.)',   len(summary.check5_results), summary.c5_breaks),
    ]
    for c, h in enumerate(['Check', 'Total', 'Breaks', 'Clean'], 1):
        cell = ws_sum.cell(3, c, h)
        cell.fill = NAVY; cell.font = WHT; cell.border = BORD
    for i, (name, total, breaks) in enumerate(summary_data, 4):
        ws_sum.cell(i, 1, name).border = BORD
        ws_sum.cell(i, 2, total).border = BORD
        ws_sum.cell(i, 3, breaks).border = BORD
        ws_sum.cell(i, 4, total - breaks).border = BORD
        fill = RED if breaks > 0 else GREEN
        for c in range(1, 5): ws_sum.cell(i, c).fill = fill
    for c, w in enumerate([35, 10, 10, 10], 1):
        ws_sum.column_dimensions[get_column_letter(c)].width = w

    if summary.warnings:
        ws_sum.cell(len(summary_data)+5, 1, 'Warnings').font = Font(bold=True, color='B03030')
        for j, w in enumerate(summary.warnings, len(summary_data)+6):
            ws_sum.cell(j, 1, w)

    # ── Check 1 sheet ──────────────────────────────────────────────────────
    ws1 = wb.create_sheet('C1 WS vs Dealer')
    _hdr(ws1, ['Pool Account','Mapin','ISIN','Instr Code','Security','Side',
               'WS Qty','Dealer Fill','Qty Diff','Is Market','Limit Px','Dealer AvgPx',
               'Price Breach','Status'],
         [18,12,14,14,30,6,12,12,12,10,12,14,13,14])
    _write_rows(ws1, [
        ([r.pool_account, r.mapin, r.isin, r.instrument_code, r.security_name,
          r.side, r.ws_qty, r.dealer_fill_qty, r.qty_diff,
          'Yes' if r.is_market else 'No', r.limit_price, r.dealer_avg_px,
          'Yes' if r.price_breach else 'No', r.status], r.status)
        for r in summary.check1_results
    ])

    # ── Check 2 sheet ──────────────────────────────────────────────────────
    ws2 = wb.create_sheet('C2 Dealer vs CN')
    _hdr(ws2, ['Mapin','Pool Account','Broker','ISIN','Security','Side',
               'Dealer Qty','Dealer AvgPx','CN Qty','CN WAP','CN No(s)',
               'Qty Match','Px Match','Status'],
         [12,18,8,14,28,6,12,14,12,14,20,10,10,20])
    _write_rows(ws2, [
        ([r.mapin, r.dealer_account, r.broker_code, r.isin, r.security_name,
          r.side, r.dealer_fill_qty, r.dealer_avg_px, r.cn_qty, r.cn_wap,
          r.cn_no, 'Y' if r.qty_match else 'N', 'Y' if r.price_match else 'N',
          r.status], r.status)
        for r in summary.check2_results
    ])

    # ── Check 3 sheet ──────────────────────────────────────────────────────
    if summary.check3_results:
        ws3 = wb.create_sheet('C3 CN vs NSDL')
        _hdr(ws3, ['CN No','ISIN','Security','Broker SEBI','Broker','UCC',
                   'Qty','Rate','In PDF','In NSDL','NSDL Status','Status'],
             [18,14,30,20,35,14,10,12,8,8,20,14])
        _write_rows(ws3, [
            ([r.cn_no, r.isin, r.security_name, r.broker_sebi, r.broker_name,
              r.ucc, r.qty, r.net_rate,
              'Y' if r.in_broker_pdf else 'N', 'Y' if r.in_nsdl else 'N',
              r.nsdl_status, r.status], r.status)
            for r in summary.check3_results
        ])
    else:
        ws3 = wb.create_sheet('C3 Skipped')
        ws3.append(['NSDL file not available for this date — C3 check skipped.'])

    # ── Check 5 sheet (optional) ───────────────────────────────────────────
    if summary.check5_results:
        ws5 = wb.create_sheet('C5 Exchange')
        _hdr(ws5, ['ISIN','Security','Side','Exchange','Exch Qty','Exch AvgPx',
                   'CN Qty','CN AvgPx','Qty Diff','Px Diff','Status'],
             [14,28,6,10,12,14,12,14,12,12,20])
        _write_rows(ws5, [
            ([r.isin, r.security, r.side, r.exchange, r.exchange_qty, r.exchange_avg_px,
              r.cn_qty, r.cn_avg_px, r.qty_diff, r.price_diff, r.status],
             r.status)
            for r in summary.check5_results
        ])

    wb.save(out_path)
    logger.info(f"Trade recon report written: {out_path}")


# ── Helpers ───────────────────────────────────────────────────────────────── #

def _add_working_days(date_str: str, n: int) -> str:
    """Add n working days to a date string, returning DD/MM/YYYY.

    Accepts DD/MM/YYYY, YYYY-MM-DD, and DD-MM-YYYY input formats — the
    same set that _serial() in write_0096_excel accepts. Previously
    only DD/MM/YYYY was accepted, which silently dropped the
    Settlement Date column when contract notes used ISO format,
    causing WS to NPE on upload (empty cell parse).
    """
    if not date_str:
        return ''
    s = str(date_str).strip()
    dt = None
    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y'):
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return ''
    added = 0
    while added < n:
        dt += timedelta(days=1)
        if dt.weekday() < 5:  # Mon-Fri
            added += 1
    return dt.strftime('%d/%m/%Y')


import re
