"""
Base classes for bank statement parsers.
"""
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import date


@dataclass
class BankTransaction:
    account_no:  str
    tran_date:   str          # DD-MMM-YYYY normalised
    description: str
    debit:       float = 0.0
    credit:      float = 0.0
    balance:     float = 0.0  # running balance after this entry (0 if not in file)
    ref_num:     str   = ''
    source:      str   = ''   # bank name


@dataclass
class BankAccount:
    account_no:           str
    account_name:         str   = ''
    opening_balance:      float = 0.0
    closing_balance:      float = 0.0
    as_on_date:           str   = ''
    source:               str   = ''
    has_opening_balance:  bool  = True
    zip_alias:            str   = ''   # HDFC: from zip filename (e.g. "GWPJ", "GWPJ0016")
    c_group:              str   = ''   # Axis: strategy code (e.g. "GOLDETEPMS")
    kotak_client_id:      str   = ''   # Kotak: numeric client ID from CSV
    source_file:          str   = ''   # Absolute path to the source file
    transactions:         List[BankTransaction] = field(default_factory=list)

    @property
    def computed_closing(self) -> float:
        """Opening + sum of credits - sum of debits."""
        if not self.transactions:
            return self.opening_balance
        net = sum(t.credit - t.debit for t in self.transactions)
        return round(self.opening_balance + net, 2)

    @property
    def total_credits(self) -> float:
        return round(sum(t.credit for t in self.transactions), 2)

    @property
    def total_debits(self) -> float:
        return round(sum(t.debit for t in self.transactions), 2)


@dataclass
class ParseResult:
    source:    str
    accounts:  List[BankAccount] = field(default_factory=list)
    error:     str = ''
    warnings:  List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error


def normalise_date(raw) -> str:
    """Convert various date formats → DD-MMM-YYYY string."""
    if raw is None:
        return ''
    from datetime import datetime
    s = str(raw).strip()
    for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%d-%b-%Y', '%d-%B-%Y',
                '%Y-%m-%d', '%d %b %Y', '%d %B %Y'):
        try:
            return datetime.strptime(s, fmt).strftime('%d-%b-%Y').upper()
        except ValueError:
            pass
    # datetime object from openpyxl
    if hasattr(raw, 'strftime'):
        return raw.strftime('%d-%b-%Y').upper()
    return s
