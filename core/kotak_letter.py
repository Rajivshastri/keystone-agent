"""
core/kotak_letter.py — generate the stamped + signed PDF letter that
goes to Kotak Securities along with each new-pool broker invitation.

Most custodians accept the standard strategy DOCX produced by
:mod:`core.pool_invitation`. Kotak requires a specific letter format
("Trading Accounts Details") on firm letterhead, signed by an
authorised signatory and stamped. The operator has uploaded both the
signature and stamp images via Settings → Branding (handled in
:mod:`core.letter_assets`); this module builds the PDF natively with
ReportLab so the deploy doesn't need LibreOffice or Word.

Layout (matches the operator's reference docx):
  1. Recipient block — Kotak Securities, BKC address.
  2. Salutation + subject.
  3. Two-line intro paragraph.
  4. Client Name + PAN.
  5. Bank Account Details — 5-column table.
  6. Depository Details — 5-column table.
  7. ~1 inch gap.
  8. Signature image + Stamp image (side-by-side, both bottom-aligned
     so the operator's stamp can overlap the signature line cleanly).

The 'For GoldStandard Wealth Private Limited' / 'Authorised
Signatories' lines from the original docx are intentionally dropped —
operator confirmed the signature + stamp images alone are sufficient.

Public entry point:
    generate(pool: dict, out_path: Path) -> Path
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

GSW_NAME = 'GoldStandard Wealth Private Limited'
GSW_PAN  = 'AALCG4181G'

KOTAK_RECIPIENT = (
    'Kotak Securities Limited',
    '27 BKC, 6th Floor, Plot No. C-27',
    '"G" Block, Bandra Kurla Complex, Bandra (East)',
    'Mumbai – 400051',
)


def _bank_row(pool: Dict[str, Any]) -> list:
    """Pull bank-table values from the pool record. Missing values
    render as a placeholder so the operator can spot what's still
    pending without the table collapsing."""
    miss = '[TBD]'
    return [
        pool.get('bank_full_name')       or miss,
        pool.get('bank_account')         or miss,
        pool.get('bank_account_type')    or 'Current',
        pool.get('bank_micr')            or miss,
        pool.get('bank_ifsc')            or miss,
    ]


def _demat_row(pool: Dict[str, Any]) -> list:
    """Demat-table values. DP Name is the human-readable depository
    label ('Kotak Securities Ltd.' for Kotak pools), Depository Name
    is the depository system (NSDL / CDSL); Beneficiary Name mirrors
    the bank account holder so the operator can verify name parity."""
    miss = '[TBD]'
    dep = (pool.get('dp', 'NSDL') or 'NSDL').upper()
    if dep in ('NSDL', 'NATIONAL SECURITIES DEPOSITORY LTD.',
               'NATIONAL SECURITIES DEPOSITORY LIMITED'):
        depository = 'NSDL'
    elif dep in ('CDSL', 'CENTRAL DEPOSITORY SERVICES LTD.',
                  'CENTRAL DEPOSITORY SERVICES (INDIA) LIMITED'):
        depository = 'CDSL'
    else:
        depository = pool.get('dp') or miss
    return [
        pool.get('dp_name') or 'Kotak Securities Ltd.',
        depository,
        pool.get('bank_account_holder') or pool.get('display_name') or miss,
        pool.get('dp_id')        or miss,
        pool.get('dp_client_id') or miss,
    ]


def generate(pool: Dict[str, Any], out_path: Path) -> Path:
    """Build the Kotak letter PDF for ``pool`` and write to ``out_path``.

    Signature + stamp images are pulled from
    :mod:`core.letter_assets`; if either is missing the letter still
    renders (with a small note in its place) so a partially-configured
    deploy doesn't block the broker invitation entirely. The operator
    can re-upload and resend.
    """
    # Local imports — reportlab is heavy and we don't want to pay the
    # cost on every Flask request.
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch, cm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image,
    )
    from reportlab.lib.enums import TA_LEFT

    from core import letter_assets as _la

    out_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        topMargin=0.9 * inch, bottomMargin=0.9 * inch,
        title=f'Trading Account Details — {pool.get("display_name", pool.get("pool_id", ""))}',
        author=GSW_NAME,
    )

    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        'body', parent=styles['Normal'],
        fontName='Helvetica', fontSize=11, leading=14,
        alignment=TA_LEFT, spaceAfter=4,
    )
    bold = ParagraphStyle('bold', parent=body, fontName='Helvetica-Bold')
    # Tighter style for inside-table body cells so long values
    # (bank name, beneficiary name) wrap within the cell rather than
    # overflowing into adjacent columns.
    cell = ParagraphStyle(
        'cell', parent=styles['Normal'],
        fontName='Helvetica', fontSize=9, leading=11,
        alignment=TA_LEFT,
    )

    story = []

    # 0. Date — top-of-letter convention. Long-form Indian style
    # (e.g. "07 May 2026") so brokers can scan the issuance date at
    # a glance without parsing locale-specific separators.
    from datetime import datetime as _dt
    today_str = _dt.now().strftime('%d %b %Y')
    story.append(Paragraph(f'<b>Date:</b> {today_str}', body))
    story.append(Spacer(1, 0.15 * inch))

    # 1. Recipient block.
    for line in KOTAK_RECIPIENT:
        story.append(Paragraph(line, body))
    story.append(Spacer(1, 0.25 * inch))

    # 2. Salutation + subject.
    story.append(Paragraph('Dear Sir,', body))
    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph('<b>Subject:</b> Trading Accounts Details', body))
    story.append(Spacer(1, 0.15 * inch))

    # 3. Intro paragraph.
    story.append(Paragraph(
        'We hereby provide the Bank and Demat details of the below '
        'mentioned trading account with you.',
        body,
    ))
    story.append(Spacer(1, 0.15 * inch))

    # 4. Client Name + PAN.
    story.append(Paragraph(f'<b>Client Name -</b> {GSW_NAME}', body))
    story.append(Paragraph(f'<b>PAN -</b> {GSW_PAN}', body))
    story.append(Spacer(1, 0.2 * inch))

    # 5. Bank Account Details.
    story.append(Paragraph('<b>Bank Account Details</b>', body))
    story.append(Spacer(1, 0.05 * inch))
    bank_headers = ['Bank Name', 'Bank A/C No', 'Account Type\n(Saving / Current)',
                    'MICR Number', 'IFSC Code']
    # Wrap each body value in a Paragraph so long text (e.g. bank
    # account holder, full bank name) wraps within its cell instead of
    # overflowing into the next column. Pre-Paragraph wrapping was the
    # root cause of cells visually running together in earlier letters.
    bank_body = [Paragraph(str(v), cell) for v in _bank_row(pool)]
    bank_data = [bank_headers, bank_body]
    bank_table = Table(bank_data,
                        colWidths=[1.45*inch, 1.6*inch, 1.3*inch, 1.05*inch, 1.0*inch])
    bank_table.setStyle(TableStyle([
        ('GRID',         (0, 0), (-1, -1), 0.5, colors.black),
        ('BACKGROUND',   (0, 0), (-1, 0),  colors.lightgrey),
        ('FONTNAME',     (0, 0), (-1, 0),  'Helvetica-Bold'),
        ('FONTNAME',     (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE',     (0, 0), (-1, -1), 9),
        ('VALIGN',       (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN',        (0, 0), (-1, -1), 'LEFT'),
        ('LEFTPADDING',  (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING',   (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 5),
    ]))
    story.append(bank_table)
    story.append(Spacer(1, 0.2 * inch))

    # 6. Depository Details.
    story.append(Paragraph('<b>Depository Details</b>', body))
    story.append(Spacer(1, 0.05 * inch))
    demat_headers = ['DP Name', 'Depository Name', 'Beneficiary Name',
                      'DP ID', 'Client ID']
    # Same wrapping treatment as the bank table — Beneficiary Name in
    # particular runs long for pool-level holders ("GOLDSTANDARD WEALTH
    # PVT LTD MYSTIC WEVA").
    demat_body = [Paragraph(str(v), cell) for v in _demat_row(pool)]
    demat_data = [demat_headers, demat_body]
    # Slightly wider Beneficiary Name column to reduce wrap to two lines
    # for the typical pool holder string.
    demat_table = Table(demat_data,
                         colWidths=[1.3*inch, 1.05*inch, 2.0*inch, 0.95*inch, 1.1*inch])
    demat_table.setStyle(TableStyle([
        ('GRID',         (0, 0), (-1, -1), 0.5, colors.black),
        ('BACKGROUND',   (0, 0), (-1, 0),  colors.lightgrey),
        ('FONTNAME',     (0, 0), (-1, 0),  'Helvetica-Bold'),
        ('FONTNAME',     (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE',     (0, 0), (-1, -1), 9),
        ('VALIGN',       (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN',        (0, 0), (-1, -1), 'LEFT'),
        ('LEFTPADDING',  (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING',   (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 5),
    ]))
    story.append(demat_table)

    # 7. ~1 inch gap before the signature + stamp row.
    story.append(Spacer(1, 1.0 * inch))

    # 8. Signature + stamp side-by-side. Each cell is the operator's
    # uploaded image, scaled to fit a 1.7-inch wide × 1.0-inch tall
    # box so the layout is predictable regardless of the source
    # resolution. Missing assets render as an italicised placeholder
    # so the operator can see at a glance that the upload is still
    # pending — letter still goes out so the broker mail isn't
    # blocked entirely.
    sig_cell = _image_cell_for('signature', body, max_w=2.0*inch, max_h=1.0*inch)
    stamp_cell = _image_cell_for('stamp',    body, max_w=2.0*inch, max_h=1.0*inch)
    sigstamp = Table([[sig_cell, stamp_cell]],
                      colWidths=[3.2*inch, 3.2*inch])
    sigstamp.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
        ('ALIGN',  (0, 0), (0, 0),    'LEFT'),
        ('ALIGN',  (1, 0), (1, 0),    'CENTER'),
        ('LEFTPADDING',  (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING',   (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 0),
    ]))
    story.append(sigstamp)

    doc.build(story)
    return out_path


def _image_cell_for(kind: str, body_style, *, max_w, max_h):
    """Return a flowable for the given asset kind, scaled to fit
    ``max_w`` × ``max_h``. Falls back to a placeholder paragraph when
    the asset hasn't been uploaded yet."""
    from reportlab.platypus import Image, Paragraph
    from PIL import Image as PILImage
    from core import letter_assets as _la

    p = _la.path_for(kind)
    if p is None:
        return Paragraph(
            f'<i>[{kind} pending — upload via Settings → Branding]</i>',
            body_style,
        )
    # Read native dimensions to compute the right scale ratio.
    try:
        with PILImage.open(p) as im:
            iw, ih = im.size
    except Exception as e:
        logger.warning(f'letter_assets read failed for {p}: {e}')
        return Paragraph(
            f'<i>[{kind} unreadable — re-upload via Settings]</i>',
            body_style,
        )
    if iw <= 0 or ih <= 0:
        return Paragraph(f'<i>[{kind} dimensions invalid]</i>', body_style)
    scale = min(max_w / iw, max_h / ih)
    return Image(str(p), width=iw * scale, height=ih * scale)
