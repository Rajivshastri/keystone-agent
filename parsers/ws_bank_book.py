"""
WS Bank Book Parser
-------------------
Parses the scheme-level bank book CSV exported from WealthSpectrum.

File structure (repeats per scheme):
  Header block:   company name, address, "BANK BOOK", date range
  Column header:  Code, Name, Bank Account, Bank Name, Set Date, Tran Account,
                  Transaction Description, Security, Buy/Sell Amount, Income,
                  Expenses, Dep/With, Balance, Custodian Account, Account Code
  Data rows:      one per client bank account + date entry
  "Bank Total":   subtotal row per account
  "Total":        scheme-level totals — closing balance + category breakdowns

The "Total" row per scheme is the primary data source for bank reconciliation.
It carries the scheme's aggregate: Buy/Sell, Income, Expenses, Dep/With, and
closing Balance.  Individual client rows are retained for metadata (account
list, custodian account resolution) but not used for financial comparison.

Output: WSBankBook with:
  - scheme_summaries: one WSSchemeSummary per scheme (from "Total" rows)
  - accounts: per-client WSAccount list (for metadata / backward compat)
"""

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────── #

@dataclass
class WSSchemeSummary:
    """Scheme-level totals from the 'Total' row of the WS Bank Book."""
    scheme_code:     str
    scheme_name:     str
    opening_balance: float = 0.0
    closing_balance: float = 0.0
    buy_sell:        float = 0.0   # Buy/Sell Amount (buy negative, sell positive)
    income:          float = 0.0   # Dividends, interest
    expenses:        float = 0.0   # Custodian charges, fees
    dep_with:        float = 0.0   # Investor deposits (+) / withdrawals (-)
    client_count:    int   = 0     # Number of unique client accounts
    source_file:     str   = ''    # Absolute path to the WS Bank Book file

    @property
    def total_credits(self) -> float:
        """Sum of all positive category amounts."""
        return round(sum(v for v in [self.buy_sell, self.income,
                                      self.expenses, self.dep_with] if v > 0), 2)

    @property
    def total_debits(self) -> float:
        """Sum of all negative category amounts (returned as positive)."""
        return round(abs(sum(v for v in [self.buy_sell, self.income,
                                          self.expenses, self.dep_with] if v < 0)), 2)

    @property
    def net(self) -> float:
        return round(self.buy_sell + self.income + self.expenses + self.dep_with, 2)

    def to_dict(self) -> dict:
        return {
            'scheme_code':     self.scheme_code,
            'scheme_name':     self.scheme_name,
            'opening_balance': self.opening_balance,
            'closing_balance': self.closing_balance,
            'buy_sell':        self.buy_sell,
            'income':          self.income,
            'expenses':        self.expenses,
            'dep_with':        self.dep_with,
            'total_credits':   self.total_credits,
            'total_debits':    self.total_debits,
            'net':             self.net,
            'client_count':    self.client_count,
        }


@dataclass
class WSTransaction:
    scheme_code:    str
    scheme_name:    str
    ws_account:     str       # e.g. "ICICI-002101019475"
    bank_prefix:    str       # e.g. "ICICI"
    account_no:     str       # e.g. "002101019475"
    set_date:       str       # DD/MM/YYYY as-is from file
    description:    str       # "For NSE Settlement"
    security:       str       # "Settlement" / "Opening Balance"
    amount:         float     # Buy/Sell Amount (negative = debit)
    balance:        float     # running balance after this entry
    custodian_acct: str       # "NSDL-IN301348-20886076"
    mapid:          str = ''  # resolved via Pool Master


@dataclass
class WSAccount:
    scheme_code:    str
    scheme_name:    str
    ws_account:     str
    bank_prefix:    str
    account_no:     str
    bank_name:      str
    opening_balance: float = 0.0
    closing_balance: float = 0.0
    custodian_acct:  str   = ''
    mapid:           str   = ''
    transactions:    List[WSTransaction] = field(default_factory=list)


@dataclass
class WSBankBook:
    date_from:        str = ''
    date_to:          str = ''
    accounts:         List[WSAccount]       = field(default_factory=list)
    scheme_summaries: List[WSSchemeSummary] = field(default_factory=list)

    def all_transactions(self) -> List[WSTransaction]:
        return [t for a in self.accounts for t in a.transactions]

    def summary_by_scheme_name(self) -> Dict[str, WSSchemeSummary]:
        """Index scheme summaries by lowercase scheme_name."""
        return {s.scheme_name.lower().strip(): s for s in self.scheme_summaries}

    def summary_by_scheme_code(self) -> Dict[str, WSSchemeSummary]:
        """Index scheme summaries by scheme_code."""
        return {s.scheme_code: s for s in self.scheme_summaries}


# ── Parser ────────────────────────────────────────────────────────────────── #

class WSBankBookParser:

    def parse_file(self, file_path: str) -> WSBankBook:
        # Dispatch to XLS parser for .xls files
        if str(file_path).lower().endswith('.xls'):
            from parsers.ws_bank_book_xls import parse_xls
            return parse_xls(file_path)
        with open(file_path, 'r', encoding='utf-8-sig', errors='replace') as fh:
            text = fh.read()
        book = self.parse_text(text)
        # Tag all scheme summaries with the source file path
        for ss in book.scheme_summaries:
            ss.source_file = str(file_path)
        return book

    def parse_text(self, text: str) -> WSBankBook:
        book = WSBankBook()
        reader = csv.reader(io.StringIO(text))
        rows   = list(reader)

        in_data     = False
        accounts: dict = {}   # ws_account_key -> WSAccount

        # Track current scheme context for capturing Total rows
        cur_scheme_code = ''
        cur_scheme_name = ''
        # Track opening balances per scheme (sum of client openings)
        scheme_opening: dict = {}       # scheme_code -> float
        # Track unique client accounts per scheme
        scheme_clients: dict = {}       # scheme_code -> set of ws_account

        for raw in rows:
            row = [c.strip() for c in raw]

            # ── Date range header ─────────────────────────────────────
            joined = ' '.join(row)
            dm = re.search(r'From\s+(\d{2}/\d{2}/\d{4})\s+To\s+(\d{2}/\d{2}/\d{4})', joined)
            if dm:
                book.date_from = dm.group(1)
                book.date_to   = dm.group(2)
                continue

            # ── Column header ─────────────────────────────────────────
            if row and row[0] == 'Code':
                in_data = True
                continue

            if not in_data:
                continue

            # ── Skip blank rows ───────────────────────────────────────
            if not row or not row[0]:
                continue

            # ── Bank Total: per-account subtotal — skip ───────────────
            if row[0] == 'Bank Total':
                continue

            # ── Total: scheme-level summary — capture it ──────────────
            if row[0] == 'Total':
                if cur_scheme_code:
                    buy_sell = self._num(row[8] if len(row) > 8 else '')
                    income   = self._num(row[9] if len(row) > 9 else '')
                    expenses = self._num(row[10] if len(row) > 10 else '')
                    dep_with = self._num(row[11] if len(row) > 11 else '')
                    closing  = self._num(row[12] if len(row) > 12 else '')
                    opening  = scheme_opening.get(cur_scheme_code, 0.0)
                    clients  = scheme_clients.get(cur_scheme_code, set())

                    book.scheme_summaries.append(WSSchemeSummary(
                        scheme_code     = cur_scheme_code,
                        scheme_name     = cur_scheme_name,
                        opening_balance = round(opening, 2),
                        closing_balance = closing,
                        buy_sell        = buy_sell,
                        income          = income,
                        expenses        = expenses,
                        dep_with        = dep_with,
                        client_count    = len(clients),
                    ))
                continue

            # ── Data row ──────────────────────────────────────────────
            try:
                scheme_code = row[0].strip()
                scheme_name = row[1].strip()
                ws_account  = row[2].strip()   # "ICICI-002101019475"
                bank_name   = row[3].strip()
                set_date    = row[4].strip()
                description = row[6].strip()
                security    = row[7].strip()
                amount_raw  = row[8].strip() if len(row) > 8 else ''
                balance_raw = row[12].strip() if len(row) > 12 else ''
                cust_acct   = row[13].strip() if len(row) > 13 else ''

            except IndexError:
                continue

            if not ws_account or not scheme_code:
                continue

            # Update current scheme context
            cur_scheme_code = scheme_code
            cur_scheme_name = scheme_name

            # Track client accounts per scheme
            scheme_clients.setdefault(scheme_code, set()).add(ws_account)

            # Parse bank prefix and account number
            if '-' in ws_account:
                bank_prefix, account_no = ws_account.split('-', 1)
            else:
                bank_prefix = bank_name.upper()[:6]
                account_no  = ws_account

            amount  = self._num(amount_raw)
            balance = self._num(balance_raw)

            # Unique key: scheme + ws_account (same account can appear in
            # multiple schemes e.g. AXIS-916010078669232 in Mystic Value + Momentum)
            acct_key = f'{scheme_code}::{ws_account}'

            if acct_key not in accounts:
                accounts[acct_key] = WSAccount(
                    scheme_code  = scheme_code,
                    scheme_name  = scheme_name,
                    ws_account   = ws_account,
                    bank_prefix  = bank_prefix.upper(),
                    account_no   = account_no,
                    bank_name    = bank_name,
                    custodian_acct = cust_acct or '',
                )

            acct = accounts[acct_key]

            # Update custodian acct if populated (may appear on txn rows only)
            if cust_acct and not acct.custodian_acct:
                acct.custodian_acct = cust_acct

            if security == 'Opening Balance':
                acct.opening_balance = balance
                acct.closing_balance = balance   # will be updated by txns
                # Accumulate scheme-level opening balance
                scheme_opening[scheme_code] = (
                    scheme_opening.get(scheme_code, 0.0) + balance)
            else:
                # Transaction row
                acct.closing_balance = balance
                if amount != 0.0 or security:
                    acct.transactions.append(WSTransaction(
                        scheme_code    = scheme_code,
                        scheme_name    = scheme_name,
                        ws_account     = ws_account,
                        bank_prefix    = bank_prefix.upper(),
                        account_no     = account_no,
                        set_date       = set_date,
                        description    = description,
                        security       = security,
                        amount         = amount,
                        balance        = balance,
                        custodian_acct = cust_acct,
                    ))

        book.accounts = list(accounts.values())
        logger.info(
            f'WSBankBook: parsed {len(book.accounts)} accounts, '
            f'{sum(len(a.transactions) for a in book.accounts)} transactions, '
            f'{len(book.scheme_summaries)} scheme summaries '
            f'({book.date_from} → {book.date_to})'
        )
        return book

    @staticmethod
    def _num(s: str) -> float:
        s = s.replace(',', '').replace('"', '').strip()
        if not s:
            return 0.0
        try:
            return float(s)
        except ValueError:
            return 0.0
