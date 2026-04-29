"""
Base parser module — defines HoldingRecord and BaseParser interface.
All source parsers inherit from BaseParser.
"""
from dataclasses import dataclass, field
from typing import List, Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class HoldingRecord:
    """A single security holding from a broker file, before WS mapping."""
    broker_code: str          # Strategy/scheme code as given by the broker
    client_id: str            # Client code within the broker's system
    security_code: str        # Broker's internal security/instrument code
    security_name: str        # Full security name
    isin: str                 # ISIN code
    logical_holding: float    # Total position (settled + pending)
    saleable_holding: float   # Position available for sale
    holding_date: str         # DD/MM/YYYY
    face_value: str = ''      # Optional face value
    security_type: str = 's'  # 's' = security (WS default)
    source: str = ''          # Which broker/source this came from


@dataclass
class ParseResult:
    """Result of parsing one broker file."""
    source: str
    file_path: str
    records: List[HoldingRecord] = field(default_factory=list)
    broker_codes_found: List[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def success(self):
        return self.error is None

    @property
    def record_count(self):
        return len(self.records)


class BaseParser:
    """
    Abstract base for all source parsers.

    Subclasses must implement parse_file().
    They receive an already-extracted file path (zip has already been unpacked).
    """

    source_name = 'base'

    def parse_file(self, file_path: str, date_str: str,
                   file_password: str = '') -> ParseResult:
        """
        Parse the extracted broker file and return holding records.

        Args:
            file_path:      Path to the extracted file (xlsx, xls, csv, etc.)
            date_str:       Date string YYYY-MM-DD (used as holding date)
            file_password:  Password to open the file (if file-level encrypted)

        Returns:
            ParseResult with list of HoldingRecord objects
        """
        raise NotImplementedError

    @staticmethod
    def format_date(date_str: str) -> str:
        """Convert YYYY-MM-DD to DD/MM/YYYY for WS upload."""
        parts = date_str.split('-')
        if len(parts) == 3:
            return f"{parts[2]}/{parts[1]}/{parts[0]}"
        return date_str

    @staticmethod
    def clean_number(value) -> float:
        """Clean Indian-formatted numbers (1,00,000 → 100000.0).

        Also handles parenthetical negatives — Indian custodian reports
        commonly write `(500)` for −500 (sells, short positions, debits).
        """
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip()
        s = s.replace(',', '').replace('₹', '').replace('Rs.', '').replace('Rs', '').strip()
        is_neg_parens = s.startswith('(') and s.endswith(')')
        if is_neg_parens:
            s = s[1:-1].strip()
        try:
            n = float(s)
            return -n if is_neg_parens else n
        except (ValueError, TypeError):
            return 0.0
