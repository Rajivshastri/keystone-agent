"""Shared date formatting helpers.

Single source of truth for the user-facing date format used across emails,
recon report titles, and any other text the operator reads. Internal date
strings used to MATCH custodian source data (HDFC bank statements, Kotak
"as on" stamps, WS OrderLog ORDER_DATE, broker contract notes, dealer
files) are NOT routed through here — those must keep their source-native
format or matching breaks.
"""


def display_date(date_str: str) -> str:
    """Convert YYYY-MM-DD to DD-MM-YYYY for display in user-facing text.

    Pass-through unchanged for inputs that don't match the YYYY-MM-DD shape,
    so callers can hand in already-formatted strings or odd values without
    crashing.
    """
    if not date_str:
        return date_str
    try:
        parts = str(date_str).split("-")
        if len(parts) == 3 and len(parts[0]) == 4:
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
    except Exception:
        pass
    return date_str
