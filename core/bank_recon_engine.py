"""
Bank Reconciliation Engine
--------------------------
Status values
-------------
MATCH          — opening + credits - debits == reported closing  (within ₹0.01)
MISMATCH       — formula does not balance
BALANCE NOTED  — closing supplied but no opening (Axis Bank)
TXN ONLY       — transactions only, no balances at all (HDFC custody file)
"""
import logging
from dataclasses import dataclass, field
from typing import List

from parsers.bank_base import BankAccount, BankTransaction

logger = logging.getLogger(__name__)

MATCH         = 'MATCH'
MISMATCH      = 'MISMATCH'
BALANCE_NOTED = 'BALANCE NOTED'
TXN_ONLY      = 'TXN ONLY'


@dataclass
class AccountReconResult:
    account_no:       str
    bank:             str
    account_name:     str
    as_on_date:       str
    opening_balance:  float
    closing_balance:  float
    total_debits:     float
    total_credits:    float
    net_movement:     float
    computed_closing: float
    balance_match:    bool
    variance:         float
    txn_count:        int
    status:           str
    transactions:     List[BankTransaction] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'account_no':       self.account_no,
            'bank':             self.bank,
            'account_name':     self.account_name,
            'as_on_date':       self.as_on_date,
            'opening_balance':  self.opening_balance,
            'closing_balance':  self.closing_balance,
            'total_debits':     self.total_debits,
            'total_credits':    self.total_credits,
            'net_movement':     self.net_movement,
            'computed_closing': self.computed_closing,
            'balance_match':    self.balance_match,
            'variance':         self.variance,
            'txn_count':        self.txn_count,
            'status':           self.status,
            'transactions': [
                {
                    'date':        t.tran_date,
                    'description': t.description,
                    'debit':       t.debit,
                    'credit':      t.credit,
                    'balance':     t.balance,
                    'ref_num':     t.ref_num,
                }
                for t in self.transactions
            ],
        }


@dataclass
class ReconSummary:
    date:    str
    results: List[AccountReconResult] = field(default_factory=list)

    @property
    def total_accounts(self) -> int:
        return len(self.results)

    @property
    def matched(self) -> int:
        return sum(1 for r in self.results if r.status == MATCH)

    @property
    def mismatched(self) -> int:
        return sum(1 for r in self.results if r.status == MISMATCH)

    @property
    def balance_noted(self) -> int:
        return sum(1 for r in self.results if r.status == BALANCE_NOTED)

    @property
    def txn_only(self) -> int:
        return sum(1 for r in self.results if r.status == TXN_ONLY)

    def to_dict(self) -> dict:
        return {
            'date':           self.date,
            'total_accounts': self.total_accounts,
            'matched':        self.matched,
            'mismatched':     self.mismatched,
            'balance_noted':  self.balance_noted,
            'txn_only':       self.txn_only,
            'results':        [r.to_dict() for r in self.results],
        }


class ReconEngine:

    TOLERANCE = 0.01  # INR

    def reconcile(self, accounts: List[BankAccount], date: str) -> ReconSummary:
        summary = ReconSummary(date=date)

        for acct in accounts:
            total_dr = round(sum(t.debit  for t in acct.transactions), 2)
            total_cr = round(sum(t.credit for t in acct.transactions), 2)
            net_mov  = round(total_cr - total_dr, 2)

            has_open  = getattr(acct, 'has_opening_balance', True)
            has_close = acct.closing_balance != 0.0

            if has_open and has_close:
                computed = round(acct.opening_balance + net_mov, 2)
                variance = round(acct.closing_balance - computed, 2)
                matched  = abs(variance) <= self.TOLERANCE
                status   = MATCH if matched else MISMATCH

            elif not has_open and has_close:
                # Axis: closing snapshot only
                computed = 0.0
                variance = 0.0
                matched  = True
                status   = BALANCE_NOTED

            else:
                # HDFC: transactions only
                computed = 0.0
                variance = 0.0
                matched  = True
                status   = TXN_ONLY

            summary.results.append(AccountReconResult(
                account_no       = acct.account_no,
                bank             = acct.source.upper(),
                account_name     = acct.account_name,
                as_on_date       = acct.as_on_date,
                opening_balance  = acct.opening_balance,
                closing_balance  = acct.closing_balance,
                total_debits     = total_dr,
                total_credits    = total_cr,
                net_movement     = net_mov,
                computed_closing = computed,
                balance_match    = matched,
                variance         = variance,
                txn_count        = len(acct.transactions),
                status           = status,
                transactions     = acct.transactions,
            ))

        logger.info(
            f'Recon: {summary.total_accounts} accounts — '
            f'{summary.matched} MATCH, {summary.mismatched} MISMATCH, '
            f'{summary.balance_noted} BAL NOTED, {summary.txn_only} TXN ONLY'
        )
        return summary
