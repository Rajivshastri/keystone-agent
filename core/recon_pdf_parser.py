"""Parser for the WS Reconciliation Statement PDF.

The Transaction Reconciliation report (downloaded by ``ws_run_recon_query``
at the end of EoD) is a multi-section PDF. Each section starts with a
centered heading; under each heading is either a table of issues or
nothing (clean). The operator's email summary needs the non-empty
sections distilled into a few lines.

Known section headings (in observed order — not all appear every day):

    Draft Transactions
    Holding Reconciliation
    Cash Reconciliation
    Price Checking
    Performance Reconciliation
    Negative Bank Balance
    Face Value Exceptions
    Not Activated and Suspended Clients
    Suspended Securities
    Allocation Inconsistency
    Security Master Exceptions
        └─ Invalid Sector Mapping (sub-section)

Approach: extract page text via pdfplumber, walk lines top-to-bottom,
identify a heading by exact-string match against KNOWN_SECTIONS, then
collect all subsequent non-blank lines until the next heading. Empty
sections (heading immediately followed by another heading) are dropped.

For sections with embedded tables, row separation is by line breaks —
pdfplumber's text extraction preserves the visual layout well enough
that "Security Code  Security Name  Module ..." style rows come out as
single lines. We don't try to parse cell-by-cell; the operator-facing
email lists rows as raw strings, which is more robust than chasing the
table layout when the report changes.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pdfplumber

logger = logging.getLogger(__name__)


KNOWN_SECTIONS: list[str] = [
    "Draft Transactions",
    "Holding Reconciliation",
    "Cash Reconciliation",
    "Price Checking",
    "Performance Reconciliation",
    "Negative Bank Balance",
    "Face Value Exceptions",
    "Not Activated and Suspended Clients",
    "Suspended Securities",
    "Allocation Inconsistency",
    "Security Master Exceptions",
    "Invalid Sector Mapping",
]

# Lines that appear in EVERY recon PDF and should never be treated as
# data — letterhead, "As Of <date>", page footers, etc.
NOISE_PREFIXES: tuple[str, ...] = (
    "Goldstandard Wealth",
    "B-508",
    "Senapati Bapat",
    "Mumbai",
    "As Of ",
)

# Tokens that appear in the column-header rows of the report's tables.
# A line is treated as a column header (and skipped) when ALL of its
# whitespace-separated tokens come from this set — that catches lines
# like "Security Code Security Name Module Modified By Modified Date"
# and "Security Symtype Detail Type AstCls Sector" without false-
# positiving on data rows.
HEADER_TOKENS: set[str] = {
    "Security", "Code", "Name", "Module", "Modified", "By", "Date",
    "Symtype", "Detail", "Type", "AstCls", "Sector",
}


def _norm(line: str) -> str:
    return " ".join(line.split())


def _is_section_heading(line: str) -> str | None:
    """If `line` matches a known section heading exactly (case-insensitive
    after normalization), return the canonical form; otherwise None.
    """
    nl = _norm(line).lower()
    for s in KNOWN_SECTIONS:
        if nl == s.lower():
            return s
    return None


def _is_column_header(line: str) -> bool:
    """True when every whitespace-separated token on the line comes
    from HEADER_TOKENS — distinguishes a column-header row like
    'Security Code Security Name Module Modified By Modified Date'
    from a data row that happens to contain a header word.
    """
    tokens = _norm(line).split()
    if not tokens:
        return False
    return all(t in HEADER_TOKENS for t in tokens)


def _is_noise(line: str) -> bool:
    nl = _norm(line)
    if not nl:
        return True
    for p in NOISE_PREFIXES:
        if nl.startswith(p):
            return True
    if _is_column_header(nl):
        return True
    # Page numbers — pdfplumber renders the footer "1", "2", ... on
    # their own line. Anything that's just digits is footer noise.
    if nl.isdigit():
        return True
    # Single-letter or 2-char fragment lines are almost always wrap
    # remnants from a value that overflowed its column (e.g. "S"
    # wrapping from "GOLDKTSPMS"). Drop them.
    if len(nl) <= 2:
        return True
    # Single-word lines without digits are almost always wraps too
    # (e.g. "Proceeds" wrapping from "Futures MTM Margin Square Up
    # Proceeds"). Real recon-row data has 5 columns, so anything
    # under 3 tokens AND no digit is wrap noise.
    tokens = nl.split()
    if len(tokens) < 3 and not any(ch.isdigit() for ch in nl):
        return True
    return False


def parse_recon_pdf(pdf_path: str | Path) -> dict[str, Any]:
    """Parse a Reconciliation Statement PDF into a section-by-section dict.

    Returns::

        {
          "ok":         True,
          "sections":   {section_name: [row_strings]},
          "summary":    "1 line per non-empty section, e.g. 'Suspended Securities (1 row)'",
          "row_count":  total data rows across all non-empty sections,
          "pdf_path":   <path>,
        }

    Empty sections are excluded from ``sections`` so the caller can
    iterate ``sections.items()`` and surface only what matters.
    """
    p = Path(pdf_path)
    if not p.exists():
        return {"ok": False, "error": f"file not found: {p}",
                "sections": {}, "summary": "", "row_count": 0,
                "pdf_path": str(p)}

    try:
        all_lines: list[str] = []
        with pdfplumber.open(str(p)) as pdf:
            for page in pdf.pages:
                txt = page.extract_text() or ""
                for raw in txt.splitlines():
                    nl = _norm(raw)
                    if nl:
                        all_lines.append(nl)
    except Exception as e:
        logger.exception(f"Failed to read PDF {p}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "sections": {}, "summary": "", "row_count": 0,
                "pdf_path": str(p)}

    # Walk lines, partition into (section_name, [rows])
    sections: dict[str, list[str]] = {}
    current: str = ""
    for line in all_lines:
        heading = _is_section_heading(line)
        if heading is not None:
            current = heading
            sections.setdefault(current, [])
            continue
        if not current:
            continue
        if _is_noise(line):
            continue
        sections[current].append(line)

    # Drop empty sections
    sections = {k: v for k, v in sections.items() if v}

    summary_lines = [f"{k} ({len(v)} row{'' if len(v) == 1 else 's'})"
                     for k, v in sections.items()]
    summary = " · ".join(summary_lines) if summary_lines \
              else "No exceptions reported."

    return {
        "ok":        True,
        "sections":  sections,
        "summary":   summary,
        "row_count": sum(len(v) for v in sections.values()),
        "pdf_path":  str(p),
    }


def format_sections_for_email(sections: dict[str, list[str]],
                               max_rows_per_section: int = 25) -> str:
    """Render a parsed section dict into plain-text email body content.

    Truncates each section to ``max_rows_per_section`` rows with a
    "(+N more)" tail so the email stays readable. The full PDF is
    attached separately for operators who want detail.
    """
    if not sections:
        return "No exceptions reported in the Reconciliation Statement."

    out: list[str] = []
    for name, rows in sections.items():
        out.append(f"{name}:")
        head = rows[:max_rows_per_section]
        for r in head:
            out.append(f"  • {r}")
        extra = len(rows) - len(head)
        if extra > 0:
            out.append(f"  … (+{extra} more — see attached PDF)")
        out.append("")
    return "\n".join(out).rstrip()
