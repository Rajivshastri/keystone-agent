from .base import HoldingRecord, ParseResult, BaseParser
from .icici import ICICIParser
from .hdfc import HDFCParser
from .kotak import KotakParser
from .axis import AxisParser

# Bank statement parsers — registered here so the holdings load route
# doesn't crash with "Unknown parser" when it encounters bank source files
# that land in raw/ alongside holding files.
from .icici_bank import ICICIBankParser
from .hdfc_bank  import HdfcBankParser
from .axis_bank  import AxisBankParser
from .kotak_bank import KotakBankParser

PARSER_REGISTRY = {
    'icici':             ICICIParser,
    'hdfc':              HDFCParser,
    'kotak':             KotakParser,
    'axis':              AxisParser,
    # Bank parsers — silently skip during holdings load (not HoldingRecord parsers)
    'icici_bank':        None,
    'hdfc_bank':         None,
    'hdfc_bank_balance': None,
    'axis_bank':         None,
    'kotak_bank':        None,
}

def get_parser(parser_name: str) -> BaseParser:
    cls = PARSER_REGISTRY.get(parser_name)
    if parser_name not in PARSER_REGISTRY:
        raise ValueError(f"Unknown parser: '{parser_name}'. "
                         f"Available: {[k for k,v in PARSER_REGISTRY.items() if v is not None]}")
    if cls is None:
        # Bank parser — not a holdings parser, return None to signal skip
        return None
    return cls()
