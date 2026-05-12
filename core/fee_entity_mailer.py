"""
core/fee_entity_mailer.py — generate per-entity fee-statement PDFs and
send them via Microsoft Graph (same path the welcome-email flow uses).

Two entry points:

    send_to_entity(entity_name, period, compute_result, ingestor) -> dict
    send_to_all (period, compute_result, ingestor) -> dict

Both fall back to a 'no email configured' skip when an entity has no
email registered in fee_entity_payments — the operator should set the
email via Settings → Fee Entities and re-trigger.

The PDF lives in a per-period folder under
``data/fee_runs/statements/{from}_{to}/`` so the operator can
audit-trail what was sent. Existing files are overwritten on resend.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _statements_dir(from_str: str, to_str: str) -> Path:
    """Per-period folder for the generated PDFs. Mirrors the
    fee_runs cache layout (KEYSTONE_DATA_DIR-rooted)."""
    base = os.environ.get('KEYSTONE_DATA_DIR') or str(
        Path(__file__).parent.parent
    )
    p = Path(base) / 'data' / 'fee_runs' / 'statements' \
        / f'{from_str}_{to_str}'
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_name(s: str) -> str:
    """Filesystem-safe entity name. Spaces → underscore, drop anything
    that isn't alnum/underscore/hyphen."""
    s = re.sub(r'\s+', '_', (s or '').strip())
    return re.sub(r'[^A-Za-z0-9_\-]', '', s) or 'entity'


def _email_body_html(entity_name: str, period: Dict[str, str],
                      view: Dict[str, Any]) -> str:
    """Short HTML body. Operators don't want a verbose template — the
    PDF carries the detail. Body just confirms: who, what period,
    headline net figure."""
    from core import branding as _br
    firm = _br.get()
    period_label = f"{period.get('from','')} to {period.get('to','')}"
    net = view.get('net_inr')
    gross = view.get('gross_inr')
    fixed = view.get('fixed_inr')
    if net is None and gross is not None:
        headline = f'Gross earnings: ₹{gross:,.2f}'
    elif fixed is None:
        headline = f'Net payable: ₹{(net or 0):,.2f} (no fixed payment configured)'
    else:
        headline = (f'Net payable: ₹{(net or 0):,.2f}  '
                    f'(Gross ₹{gross:,.2f} − Fixed ₹{fixed:,.2f})')
    return (
        f'<p>Dear {entity_name},</p>'
        f'<p>Please find attached the fee statement for the period '
        f'<b>{period_label}</b>.</p>'
        f'<p style="font-size:14px"><b>{headline}</b></p>'
        f'<p style="color:#666;font-size:13px">'
        f'This is an automated statement from {firm["firm_name"]}. '
        f'Reply to this email if you have any questions.</p>'
        f'<p>Regards,<br>{firm["firm_name"]}</p>'
    )


def send_to_entity(entity_name: str,
                   period: Dict[str, str],
                   compute_result: Dict[str, Any],
                   ingestor,
                   *,
                   override_recipients: Optional[List[str]] = None,
                   ) -> Dict[str, Any]:
    """Generate the PDF for ``entity_name`` and email it.

    Recipients resolution:
      - When ``override_recipients`` is supplied, it wins (used by the
        app endpoint to route GSW → admin_emails, since the firm itself
        has no per-entity email field; also used for ad-hoc operator
        test sends).
      - Otherwise, look up the entity's registered email in
        fee_entity_payments. Empty → skip with 'no email registered'.

    Returns ``{ok, entity, recipients, pdf_path, message}`` on success
    and ``{ok: False, entity, reason}`` on skip / failure (so the
    caller can fold the result into a bulk-send summary)."""
    from core.fee_entity_pdf import build_view, generate
    from core.fee_entity_payments import get_entity_email
    from core import branding as _br

    name = (entity_name or '').strip()
    if not name:
        return {'ok': False, 'entity': name, 'reason': 'name required'}

    if override_recipients is not None:
        recipients = [r.strip() for r in override_recipients
                       if r and isinstance(r, str) and r.strip()]
        if not recipients:
            return {'ok': False, 'entity': name,
                    'reason': 'override list resolved to no recipients'}
    else:
        email = get_entity_email(name)
        if not email:
            return {'ok': False, 'entity': name,
                    'reason': 'no email registered'}
        recipients = [email]

    if not ingestor or not ingestor.is_configured():
        return {'ok': False, 'entity': name,
                'reason': 'email ingestor not configured'}

    view = build_view(name, compute_result)

    from_str = period.get('from') or ''
    to_str   = period.get('to')   or ''
    pdf_dir  = _statements_dir(from_str, to_str)
    pdf_path = pdf_dir / f'FeeStatement_{_safe_name(name)}_{from_str}_{to_str}.pdf'
    try:
        generate(name, period, view, pdf_path)
    except Exception as e:
        logger.exception(f'fee-entity PDF generation failed for {name}')
        return {'ok': False, 'entity': name,
                'reason': f'PDF generation failed: {e}'}

    firm = _br.get()
    subject = f"{firm['firm_short_name']} — Fee Statement for {from_str} to {to_str}"
    body_html = _email_body_html(name, period, view)
    try:
        res = ingestor.send_simple_email(
            subject=subject,
            body_html=body_html,
            recipients=recipients,
            attachments=[{
                'name':         pdf_path.name,
                'path':         str(pdf_path),
                'content_type': 'application/pdf',
            }],
        )
    except Exception as e:
        logger.exception(f'fee-entity email send failed for {name}')
        return {'ok': False, 'entity': name, 'recipients': recipients,
                'pdf_path': str(pdf_path), 'reason': str(e)}

    if not res.get('ok'):
        return {'ok': False, 'entity': name, 'recipients': recipients,
                'pdf_path': str(pdf_path),
                'reason': res.get('message', 'send failed')}

    return {'ok': True, 'entity': name, 'recipients': recipients,
            'pdf_path': str(pdf_path),
            'gross_inr': view.get('gross_inr'),
            'net_inr':   view.get('net_inr'),
            'message':   res.get('message')}


def send_to_all(period: Dict[str, str],
                compute_result: Dict[str, Any],
                ingestor,
                *,
                recipient_overrides: Optional[Dict[str, List[str]]] = None,
                ) -> Dict[str, Any]:
    """Mail every entity that (a) appears in the by_earner roll-up for
    this period AND (b) has registered recipients (either a per-entity
    email OR an override list supplied by the caller).

    ``recipient_overrides`` keys are entity-name-lowercase. The app
    layer uses this to route GSW → admin_emails (the firm itself has
    no per-entity email field).

    Entities with no email AND no override are recorded under
    ``skipped`` so the operator can see who was missed without having
    to compare lists.
    """
    from core.fee_entity_payments import load_payments

    overrides = {(k or '').strip().lower(): v
                 for k, v in (recipient_overrides or {}).items()
                 if v}

    by_earner = compute_result.get('by_earner') or []
    # Distinct entity names from the by_earner table — same as the
    # 'send' button list the UI shows.
    seen: Dict[str, bool] = {}
    names: List[str] = []
    for e in by_earner:
        nm = (e.get('name') or '').strip()
        if not nm or nm.lower() in seen:
            continue
        seen[nm.lower()] = True
        names.append(nm)

    payments = load_payments()

    sent:    List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    failed:  List[Dict[str, Any]] = []
    for nm in names:
        ov = overrides.get(nm.lower())
        if ov:
            res = send_to_entity(nm, period, compute_result, ingestor,
                                  override_recipients=ov)
        else:
            rec = payments.get(nm) or {}
            email = (rec.get('email') or '').strip()
            if not email:
                skipped.append({'entity': nm, 'reason': 'no email registered'})
                continue
            res = send_to_entity(nm, period, compute_result, ingestor)
        if res.get('ok'):
            sent.append(res)
        elif res.get('reason') == 'no email registered':
            skipped.append({'entity': nm, 'reason': 'no email registered'})
        else:
            failed.append(res)

    return {'ok': True, 'sent': sent, 'skipped': skipped, 'failed': failed,
            'totals': {'sent': len(sent), 'skipped': len(skipped),
                       'failed': len(failed)}}
