"""
core/fee_entity_pdf.py — per-entity fee statement PDF.

Reads a single entity's slice out of a compute_fees_daily result and
renders a PDF the operator can email. Sections:

  1. Header band — firm logo + 'Fee Statement'.
  2. Recipient + period meta.
  3. Summary card — Gross / Fixed / Net.
  4. Per-role pool tables — one section per role the entity occupies
     (FM / Distributor / Residual / GSW). Each row shows pool, scheme,
     period, AUM, share %, earnings.
  5. Footer — confidentiality note + generated-at timestamp.

The slicing logic in :func:`build_view` is the same the mailer uses to
decide what to attach — keep both call sites aligned by routing the
mailer through the same helpers.

Public entry point:

    generate(entity_name, period, view, out_path) -> Path

``view`` is the dict produced by :func:`build_view`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


GSW_NAME = 'GoldStandard Wealth'

# Role display order on the PDF — matches the in-app earner table.
ROLE_LABELS = (
    ('gsw',         'GoldStandard Wealth (firm)'),
    ('fm',          'Fund Manager'),
    ('distributor', 'Distributor / Intermediary'),
    ('residual',    'Residual'),
)


def build_view(entity_name: str,
               compute_result: Dict[str, Any],
               by_earner_row_for_entity: Optional[Dict[str, Any]] = None
               ) -> Dict[str, Any]:
    """Slice ``compute_result`` to the rows where ``entity_name``
    earns. Returns a dict shaped for :func:`generate`:

        {
            'entity_name':  'Nirman Sheth',
            'roles':        [{'role': 'fm',
                              'role_label': 'Fund Manager',
                              'rows': [{...}, ...],
                              'subtotal_inr': float}, ...],
            'gross_inr':    float,
            'fixed_inr':    float | None,
            'net_inr':      float | None,
        }

    The fixed_inr / net_inr come from compute_result['by_earner'] when
    an exact match is found (single-role) — multi-role entities are
    summed across role rows. Mirrors the UI's parent-row total."""
    target = (entity_name or '').strip()
    if not target:
        return {'entity_name': '', 'roles': [], 'gross_inr': 0.0,
                'fixed_inr': None, 'net_inr': None}
    target_lc = target.lower()

    by_account = compute_result.get('by_account') or []
    role_groups: Dict[str, List[Dict[str, Any]]] = {
        'gsw': [], 'fm': [], 'distributor': [], 'residual': [],
    }

    def _push(role: str, li: Dict[str, Any], pct: float, inr: float):
        role_groups[role].append({
            'account_code': li.get('account_code') or '',
            'client_name':  li.get('client_name')  or '',
            'scheme_name':  li.get('scheme_name')  or '',
            'period_from':  li.get('period_from')  or '',
            'period_to':    li.get('period_to')    or '',
            'period_days':  li.get('period_days')  or 0,
            'avg_aum':      float(li.get('avg_aum') or 0),
            'share_pct':    float(pct or 0),
            'earnings_inr': float(inr or 0),
        })

    for li in by_account:
        # GSW is always the firm; treat any entity name lookup against
        # GSW only when the operator chose to mail the firm itself.
        if target_lc == GSW_NAME.lower() and (li.get('gsw_inr') or 0):
            _push('gsw', li, li.get('gsw_pct'), li.get('gsw_inr'))
        if (li.get('fm_name') or '').strip().lower() == target_lc \
                and (li.get('fm_inr') or 0):
            _push('fm', li, li.get('fm_pct'), li.get('fm_inr'))
        if (li.get('distributor_name') or '').strip().lower() == target_lc \
                and (li.get('distributor_inr') or 0):
            _push('distributor', li, li.get('distributor_pct'),
                  li.get('distributor_inr'))
        if (li.get('residual_name') or '').strip().lower() == target_lc \
                and (li.get('residual_inr') or 0):
            _push('residual', li, li.get('residual_pct'),
                  li.get('residual_inr'))

    roles_view: List[Dict[str, Any]] = []
    gross = 0.0
    for role_key, role_label in ROLE_LABELS:
        rows = role_groups.get(role_key) or []
        if not rows:
            continue
        sub = sum(r['earnings_inr'] for r in rows)
        gross += sub
        rows.sort(key=lambda r: (-r['earnings_inr'], r['scheme_name'].lower()))
        roles_view.append({
            'role':         role_key,
            'role_label':   role_label,
            'rows':         rows,
            'subtotal_inr': sub,
        })

    # Fixed / net come from by_earner (one entry per (name, role)).
    # Sum fixed across rows where this entity is named — by construction
    # the JSON store has one fixed value per ENTITY (not per role) so
    # any matching row carries the same number; take the first.
    fixed_inr: Optional[float] = None
    net_inr:   Optional[float] = None
    by_earner = compute_result.get('by_earner') or []
    matches = [e for e in by_earner
               if (e.get('name') or '').strip().lower() == target_lc]
    if matches:
        # Single-row matches carry net directly; multi-row need
        # gross−fixed. Both: fixed is shared across rows, so pick any.
        fixed_inr = matches[0].get('fixed_inr')
        if fixed_inr is None:
            net_inr = gross
        else:
            net_inr = gross - float(fixed_inr)

    return {
        'entity_name':  target,
        'roles':        roles_view,
        'gross_inr':    round(gross, 2),
        'fixed_inr':    (round(float(fixed_inr), 2)
                          if fixed_inr is not None else None),
        'net_inr':      (round(float(net_inr), 2)
                          if net_inr is not None else None),
    }


def generate(entity_name: str,
             period: Dict[str, str],
             view: Dict[str, Any],
             out_path: Path) -> Path:
    """Render the per-entity fee statement PDF to ``out_path``.

    ``period`` = ``{'from': 'YYYY-MM-DD', 'to': 'YYYY-MM-DD'}``.
    Empty role list still produces a valid PDF (with a 'no earnings
    in this period' line) so the operator gets a clear send-confirmation
    rather than a silent 'nothing to attach'.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image,
    )
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER

    from core import branding as _br

    out_path.parent.mkdir(parents=True, exist_ok=True)
    firm = _br.get()

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f'Fee Statement — {entity_name}',
        author=firm['firm_name'],
    )

    styles = getSampleStyleSheet()
    body = ParagraphStyle('body', parent=styles['Normal'],
                          fontName='Helvetica', fontSize=10, leading=13,
                          alignment=TA_LEFT)
    body_r = ParagraphStyle('body_r', parent=body, alignment=TA_RIGHT)
    h_title = ParagraphStyle('h_title', parent=body, fontName='Helvetica-Bold',
                              fontSize=18, leading=22)
    h_section = ParagraphStyle('h_section', parent=body,
                                fontName='Helvetica-Bold',
                                fontSize=12, leading=16, spaceBefore=10,
                                spaceAfter=4)
    meta = ParagraphStyle('meta', parent=body, fontSize=9, textColor=colors.grey,
                           leading=11)
    foot = ParagraphStyle('foot', parent=body, fontSize=8, textColor=colors.grey,
                           alignment=TA_CENTER)

    story = []

    # 1. Header band — logo (left) + title block (right).
    logo_path = Path(__file__).parent.parent / 'static' / 'logo.png'
    logo_cell = _logo_image(logo_path, max_w=1.4 * inch, max_h=0.7 * inch)
    title_block = [
        Paragraph(firm['firm_name'], h_section),
        Paragraph('Fee Statement', h_title),
    ]
    header = Table(
        [[logo_cell, title_block]],
        colWidths=[1.6 * inch, 4.6 * inch],
    )
    header.setStyle(TableStyle([
        ('VALIGN',       (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING',  (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING',   (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 0),
    ]))
    story.append(header)
    story.append(Spacer(1, 0.15 * inch))
    story.append(_hr(colors.lightgrey))
    story.append(Spacer(1, 0.12 * inch))

    # 2. Recipient + period meta.
    period_label = f"{period.get('from','')} to {period.get('to','')}"
    story.append(Paragraph(f'<b>Recipient:</b> {entity_name}', body))
    story.append(Paragraph(f'<b>Period:</b> {period_label}', body))
    story.append(Spacer(1, 0.12 * inch))

    # 3. Summary card — Gross / Fixed / Net.
    gross = view.get('gross_inr') or 0.0
    fixed = view.get('fixed_inr')
    net   = view.get('net_inr')
    summary_rows = [
        ['Gross earnings',  _inr(gross, prefix=True)],
        ['Fixed payment',   _inr(fixed, prefix=True) if fixed is not None
                              else '—  (no fixed payment configured)'],
        ['Net payable',     _inr(net, prefix=True) if net is not None
                              else _inr(gross, prefix=True)],
    ]
    summary_tbl = Table(summary_rows, colWidths=[2.3 * inch, 2.3 * inch])
    summary_tbl.setStyle(TableStyle([
        ('BOX',          (0, 0), (-1, -1), 0.5,  colors.grey),
        ('LINEBELOW',    (0, 0), (-1, -2), 0.25, colors.lightgrey),
        ('FONTNAME',     (0, 0), (0, -1),  'Helvetica'),
        ('FONTNAME',     (1, 0), (1, -1),  'Helvetica'),
        ('FONTNAME',     (0, -1),(-1, -1), 'Helvetica-Bold'),
        ('BACKGROUND',   (0, -1),(-1, -1), colors.HexColor('#F4ECD8')),
        ('FONTSIZE',     (0, 0), (-1, -1), 10),
        ('ALIGN',        (1, 0), (1, -1),  'RIGHT'),
        ('LEFTPADDING',  (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING',   (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 6),
    ]))
    story.append(summary_tbl)
    story.append(Spacer(1, 0.18 * inch))

    # 4. Per-role pool tables.
    roles = view.get('roles') or []
    if not roles:
        story.append(Paragraph(
            '<i>No earnings recorded for this entity in the selected period.</i>',
            body))
    for r in roles:
        story.append(Paragraph(
            f"{r['role_label']} — {len(r['rows'])} pool"
            f"{'s' if len(r['rows']) != 1 else ''}",
            h_section))
        story.append(_pool_table(r['rows'], r['subtotal_inr']))
        story.append(Spacer(1, 0.15 * inch))

    # 5. Footer — confidentiality + generated-at.
    story.append(Spacer(1, 0.2 * inch))
    story.append(_hr(colors.lightgrey))
    gen_at = datetime.now(timezone.utc).astimezone().strftime('%d %b %Y, %H:%M %Z')
    story.append(Paragraph(
        f'Confidential — for internal use of the named recipient only. '
        f'Generated {gen_at} • {firm["firm_name"]}',
        foot))

    doc.build(story)
    return out_path


# ── Helpers ─────────────────────────────────────────────────────────────── #

def _inr(v: Optional[float], *, prefix: bool = False) -> str:
    """Indian-grouped rupee format. ``prefix=True`` prepends 'Rs '
    (used in summary cards where there's no column header to carry the
    currency label). Helvetica — ReportLab's default — has no glyph for
    U+20B9 (₹) so we fall back to 'Rs ' rather than render an ugly
    missing-glyph block. The text 'Rs' is universally recognised in
    Indian finance documents."""
    if v is None:
        return '—'
    try:
        n = float(v)
    except (TypeError, ValueError):
        return '—'
    neg = n < 0
    n = abs(n)
    int_part = int(n)
    frac = round(n - int_part, 2)
    s = str(int_part)
    if len(s) <= 3:
        grouped = s
    else:
        last3 = s[-3:]
        rest = s[:-3]
        # Group rest in pairs from the right.
        pairs = []
        while len(rest) > 2:
            pairs.append(rest[-2:])
            rest = rest[:-2]
        if rest:
            pairs.append(rest)
        grouped = ','.join(reversed(pairs)) + ',' + last3
    paise = f'{frac:.2f}'.split('.')[1]
    body = f'{grouped}.{paise}'
    if prefix:
        body = f'Rs {body}'
    return f'-{body}' if neg else body


def _pct(v: Optional[float]) -> str:
    if v is None:
        return '—'
    try:
        return f'{float(v):.4f}%'
    except (TypeError, ValueError):
        return '—'


def _logo_image(path: Path, *, max_w, max_h):
    """Image flowable for the logo, scaled to fit. Empty paragraph
    when missing so the title-block still renders alone."""
    from reportlab.platypus import Image, Paragraph
    from reportlab.lib.styles import getSampleStyleSheet
    if not path.exists():
        return Paragraph('', getSampleStyleSheet()['Normal'])
    try:
        from PIL import Image as PILImage
        with PILImage.open(path) as im:
            iw, ih = im.size
    except Exception:
        return Image(str(path), width=max_w, height=max_h)
    if iw <= 0 or ih <= 0:
        return Paragraph('', getSampleStyleSheet()['Normal'])
    scale = min(max_w / iw, max_h / ih)
    return Image(str(path), width=iw * scale, height=ih * scale)


def _hr(col):
    """A 1pt horizontal rule via a single-cell table (Spacer can't draw
    lines and HRFlowable needs extra setup that's overkill here)."""
    from reportlab.platypus import Table, TableStyle
    t = Table([['']], colWidths=[6.6 * 72])    # 6.6 inches
    t.setStyle(TableStyle([
        ('LINEABOVE',    (0, 0), (-1, 0), 0.5, col),
        ('LEFTPADDING',  (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING',   (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 0),
    ]))
    return t


def _pool_table(rows: List[Dict[str, Any]], subtotal: float):
    """Six-column pool table: Account, Scheme/Client, Period, AUM (avg),
    Share %, Earnings. Subtotal row at the bottom.

    Column widths sum to 6.85" — the A4 usable width with 0.7" margins.
    Cells render currency without the 'Rs ' prefix; the column header
    'Avg AUM (Rs)' / 'Earnings (Rs)' carries the label, keeping cells
    compact enough that long Indian-grouped numbers like
    46,94,88,456.16 fit without wrapping."""
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import Table, TableStyle

    header = ['Account', 'Scheme / Client', 'Period', 'Avg AUM (Rs)',
              'Share %', 'Earnings (Rs)']
    body_rows: List[List[Any]] = [header]
    for r in rows:
        # Compact period: 'YYYY-MM-DD–YYYY-MM-DD (Nd)'. en-dash (–)
        # is narrower than ' → ' and avoids any glyph-availability
        # surprise the way the unicode arrow can.
        period_str = (f"{r['period_from']}–{r['period_to']} "
                      f"({r['period_days']}d)")
        body_rows.append([
            r['account_code'],
            r['client_name'] or r['scheme_name'],
            period_str,
            _inr(r['avg_aum']),
            _pct(r['share_pct']),
            _inr(r['earnings_inr']),
        ])
    body_rows.append(['', '', '', '', 'Subtotal', _inr(subtotal)])

    tbl = Table(body_rows, colWidths=[
        0.75 * inch,  # Account
        1.45 * inch,  # Scheme / Client
        2.00 * inch,  # Period (longest cell content — 'YYYY-MM-DD–YYYY-MM-DD (Nd)')
        1.20 * inch,  # Avg AUM (Rs) — fits 'NN,NN,NN,NN,NNN.NN'
        0.55 * inch,  # Share %
        0.90 * inch,  # Earnings (Rs)
    ])
    tbl.setStyle(TableStyle([
        ('GRID',         (0, 0), (-1, -2), 0.25, colors.lightgrey),
        ('BACKGROUND',   (0, 0), (-1, 0),  colors.HexColor('#0B1E3A')),
        ('TEXTCOLOR',    (0, 0), (-1, 0),  colors.white),
        ('FONTNAME',     (0, 0), (-1, 0),  'Helvetica-Bold'),
        ('FONTNAME',     (0, 1), (-1, -2), 'Helvetica'),
        ('FONTSIZE',     (0, 0), (-1, -1), 8),
        ('VALIGN',       (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN',        (3, 0), (5, -1),  'RIGHT'),
        ('ALIGN',        (0, 0), (2, -1),  'LEFT'),
        ('LEFTPADDING',  (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING',   (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 4),
        # Subtotal row.
        ('LINEABOVE',    (4, -1), (-1, -1), 0.5, colors.black),
        ('FONTNAME',     (4, -1), (-1, -1), 'Helvetica-Bold'),
        ('TOPPADDING',   (0, -1), (-1, -1), 5),
    ]))
    return tbl
