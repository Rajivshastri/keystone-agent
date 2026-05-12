"""
core/cml_email_parser.py — extract UCC / CP / exchange-mapping from
the body of a custodian's CML email.

The pool-creation flow accepts a CML PDF (and Schedule-A) but custody
banks vary in how they communicate the codes that aren't in the PDF
itself:

  - **Axis Bank**  — UCC + CP Code in the email body. Sends TWO CMLs
                     per strategy (NSE + BSE); the body identifies
                     which attachment corresponds to which exchange.
                     The pool form takes the NSE values (per design).
  - **HDFC Bank**  — UCC in the email body; CP Code arrives later in
                     a separate communication after the demat / bank
                     accounts are opened. The pool creator's
                     pending-CP workflow handles the wait.
  - **ICICI Bank** — neither UCC nor CP comes from the bank's email.
                     UCC is operator-initiated (not in the email);
                     CP is constant per firm (configured once on the
                     Custodians screen as default_cp_code).
  - **Other**      — defaults to a no-op return; operator types every
                     code by hand.

Body text is HTML — we strip tags before matching. Patterns are
defensive (case-insensitive, tolerate extra whitespace, optional
colons / dashes / parentheses around values) so a small layout
tweak in the bank's email template doesn't silently break extraction.

Public surface:

    parse(bank_short, body, attachment_names) -> dict

where ``bank_short`` is the WS short label ('AXIS' / 'HDFC' / 'ICICI'
/ ...) and ``attachment_names`` is the list of filenames (used by
the Axis parser to map each filename to its exchange).

Returned dict keys (any may be absent / empty):

    {
      'ucc':                str,
      'cp_code':            str,
      'exchange_per_attachment': {filename: 'NSE'|'BSE'|''},
      'preferred_attachment':   filename | '',  # the one to feed the
                                                # pool form (Axis: NSE)
      'parser':             'axis'|'hdfc'|'icici'|'',
      'notes':              [str, ...],         # operator-readable
                                                # diagnostics
    }
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Public entry point ───────────────────────────────────────────────────── #

def parse(bank_short: str,
           body: str,
           attachment_names: Optional[List[str]] = None) -> Dict:
    """Run the per-bank parser appropriate for ``bank_short``.

    Empty / None body returns an empty result. Unknown bank short
    label also returns empty (no parser available — operator types
    every code).
    """
    out: Dict = {
        'ucc': '',
        'cp_code': '',
        'exchange_per_attachment': {},
        'preferred_attachment': '',
        'parser': '',
        'notes': [],
    }
    if not body:
        return out
    short = (bank_short or '').strip().upper()
    text = _strip_html(body)
    if short == 'AXIS':
        return _parse_axis(text, attachment_names or [])
    if short == 'HDFC':
        return _parse_hdfc(text, attachment_names or [])
    if short == 'ICICI':
        return _parse_icici(text, attachment_names or [])
    out['notes'].append(f"No CML email parser for custodian {short!r} — operator enters codes manually.")
    return out


# ── Per-bank parsers ─────────────────────────────────────────────────────── #

def _parse_axis(text: str, attachment_names: List[str]) -> Dict:
    """Axis CML email body carries:
        - Strategy UCC code (single value)
        - CP Code unique to Axis for this strategy
        - One CML attachment for NSE, one for BSE — body specifies
          which is which by name.

    Operator's pool form takes the NSE values per design.
    """
    out: Dict = {
        'ucc': _extract_ucc(text),
        'cp_code': _extract_cp(text),
        'exchange_per_attachment': {},
        'preferred_attachment': '',
        'parser': 'axis',
        'notes': [],
    }
    # Map attachment_name → 'NSE' | 'BSE' by scanning the body for a
    # nearby exchange tag. Two strategies: explicit "for NSE" sentence
    # near the filename, or the filename itself contains NSE/BSE.
    for fname in attachment_names:
        ex = _exchange_for_filename(text, fname)
        out['exchange_per_attachment'][fname] = ex
    # Pick the NSE attachment as preferred — pool form's data binding
    # takes its UCC/CP/values from this one. If neither attachment is
    # tagged NSE, the operator picks manually (no preferred).
    nse_files = [f for f, ex in out['exchange_per_attachment'].items() if ex == 'NSE']
    if nse_files:
        out['preferred_attachment'] = nse_files[0]
    elif len(attachment_names) == 1:
        # Single attachment — assume operator dropped the NSE one.
        out['preferred_attachment'] = attachment_names[0]
    else:
        out['notes'].append(
            'Axis: could not identify which attachment is NSE — '
            'pool form will use the first dropped PDF.')
    return out


def _parse_hdfc(text: str, attachment_names: List[str]) -> Dict:
    """HDFC CML email body carries:
        - Strategy UCC code (created by HDFC, sent in the email)
        - NOT the CP Code (that arrives later in a separate
          communication after the bank/demat accounts are opened).

    Operator routes HDFC pools through the pending-CP workflow on
    the pool creator: stash the parsed values, wait for CP, resume.
    """
    return {
        'ucc': _extract_ucc(text),
        'cp_code': '',
        'exchange_per_attachment': {f: '' for f in attachment_names},
        'preferred_attachment': attachment_names[0] if attachment_names else '',
        'parser': 'hdfc',
        'notes': [
            'HDFC: CP Code is not in the CML email — wait for the '
            'separate CP notification, then resume pool creation '
            'via the pending-CP workflow.',
        ],
    }


def _parse_icici(text: str, attachment_names: List[str]) -> Dict:
    """ICICI CML email body has neither the UCC nor the CP Code.

    UCC is operator-initiated (Keystone's call, not the bank's). CP
    is constant for the firm (set once as Custodians → ICICI →
    Default CP Code; auto-fills via _pcOnCustodianChange).
    """
    return {
        'ucc': '',
        'cp_code': '',
        'exchange_per_attachment': {f: '' for f in attachment_names},
        'preferred_attachment': attachment_names[0] if attachment_names else '',
        'parser': 'icici',
        'notes': [
            'ICICI: UCC is operator-entered (the firm initiates the '
            'request — not in the email). CP Code auto-fills from '
            'the Custodians screen Default CP Code.',
        ],
    }


# ── Shared regex helpers ─────────────────────────────────────────────────── #

_HTML_TAG_RE = re.compile(r'<[^>]+>')
_NBSP_RE     = re.compile(r'&nbsp;|&#160;', re.IGNORECASE)
_ENTITY_RE   = re.compile(r'&[a-zA-Z]{2,8};|&#\d{2,5};')
_WS_RE       = re.compile(r'[ \t\r]+')


def _strip_html(html: str) -> str:
    """Strip HTML tags and decode common entities so regex patterns
    can operate on flat text. Whitespace is normalised but newlines
    are kept (some patterns use them as anchors)."""
    if not html:
        return ''
    s = _HTML_TAG_RE.sub(' ', html)
    s = _NBSP_RE.sub(' ', s)
    s = _ENTITY_RE.sub(' ', s)
    # Collapse run-on whitespace within a line (preserve newlines for
    # near-match exchange detection).
    s = '\n'.join(_WS_RE.sub(' ', line).strip() for line in s.splitlines())
    return s


# UCC patterns — defensive: tolerate "UCC", "Unique Client Code",
# "Trading UCC", "SEBI UCC", optional colon/dash, optional parens.
_UCC_PATTERNS = [
    r'(?:Unique\s+Client\s+Code|SEBI\s+UCC|Trading\s+UCC|\bUCC\s*Code|\bUCC)\s*[:\-]?\s*([A-Z0-9]{4,16})',
]


def _extract_ucc(text: str) -> str:
    for pat in _UCC_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            v = m.group(1).strip().upper()
            # Sanity: real UCC codes are alphanumeric, not all-digits-of-1
            # length and not a generic word like "CODE" picked up by a
            # too-greedy pattern.
            if v and v not in {'CODE', 'CLIENT', 'TRADING'}:
                return v
    return ''


# CP Code patterns — common labels: "CP Code", "CP ID", "Custodian
# Participant Code", "Clearing Participant Code".
_CP_PATTERNS = [
    r'(?:CP\s*Code|CP\s*ID|Custodian\s+Participant\s+Code|Clearing\s+Participant\s+Code)\s*[:\-]?\s*([A-Z0-9\-]{3,20})',
]


def _extract_cp(text: str) -> str:
    for pat in _CP_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            v = m.group(1).strip().upper().rstrip('-')
            if v and v not in {'CODE', 'ID'}:
                return v
    return ''


def _exchange_for_filename(text: str, fname: str) -> str:
    """Decide whether ``fname`` corresponds to NSE or BSE based on:
      1. The filename itself ('XX_NSE_YY.pdf' / 'XX_BSE_YY.pdf').
      2. A nearby reference in the body — e.g. a sentence like
         'NSE CML attached as <fname>' or 'attached <fname> for BSE'.

    Returns 'NSE' / 'BSE' / '' (unknown).
    """
    fn_up = fname.upper()
    # 1. Filename-based.
    if 'NSE' in fn_up:
        return 'NSE'
    if 'BSE' in fn_up:
        return 'BSE'
    # 2. Body-based — find filename in body and check ±200 chars window.
    idx = text.upper().find(fn_up)
    if idx < 0:
        return ''
    window = text[max(0, idx - 200): idx + len(fname) + 200].upper()
    nse_hits = window.count('NSE')
    bse_hits = window.count('BSE')
    if nse_hits and not bse_hits:
        return 'NSE'
    if bse_hits and not nse_hits:
        return 'BSE'
    if nse_hits > bse_hits:
        return 'NSE'
    if bse_hits > nse_hits:
        return 'BSE'
    return ''
