"""
core/pool_invitation.py — broker invitation email after pool creation.

After a successful pool creation on WS, every broker with at least one
``email_to`` recipient gets a "New Trading Code" mail. The body is the
fixed text from the spec; two attachments ride along:

  1. The CML PDF the operator dropped during create (stored under
     ``data/{date}/raw/cml_uploads/<pool_id>_cml.pdf`` by save_uploads).
  2. A generated strategy DOCX, built from the sample template at
     ``C:/Users/Administrator/Downloads/AI Portfolio Details.docx`` with
     three tables: client identity, depository info, bank info.

Fire-and-forget — failure to send doesn't reverse the pool creation;
the operator can resend from the pool's edit page.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# GoldStandard Wealth's PAN — referenced in the email body and the
# strategy doc. Static for now; promote to config if a sibling entity
# ever needs invitations under a different PAN.
GSW_PAN = 'AALCG4181G'
GSW_NAME = 'GoldStandard Wealth Private Limited'

EMAIL_SUBJECT_TEMPLATE = "New Trading Code - {pool_name}"

EMAIL_BODY_TEMPLATE = (
    "Dear Team,\n\n"
    "Please arrange to open a new trading account in the name of {pool_name}.\n\n"
    "Please note that the bank and demat accounts for this are under "
    "GoldStandard Wealth Private Limited's PAN: {pan}, and the KYC details "
    "are also the same as those of GoldStandard Wealth Private Limited.\n\n"
    "Regards,\n"
    "GoldStandard Wealth"
)


# ── Upload persistence ─────────────────────────────────────────────────── #

def upload_dir(data_root: Path) -> Path:
    """Where parse-cml / parse-schedule-a stash temp uploads keyed by
    UUID, before they get moved into the date-organised tree at
    create-time."""
    p = data_root / '_pool_creator_uploads'
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_temp_upload(data_root: Path, src_path: str,
                      kind: str = 'cml') -> str:
    """Move ``src_path`` to a permanent slot under data/_pool_creator_uploads/
    keyed by UUID. Returns the token; the file lives at
    upload_dir/<kind>_<token>.pdf."""
    token = uuid.uuid4().hex[:16]
    dst = upload_dir(data_root) / f'{kind}_{token}.pdf'
    shutil.copyfile(src_path, dst)
    return token


def resolve_temp_upload(data_root: Path, token: str,
                        kind: str = 'cml') -> Optional[Path]:
    """Resolve a token returned by save_temp_upload back to its file
    path. Returns None if the file's been pruned."""
    if not token:
        return None
    p = upload_dir(data_root) / f'{kind}_{token}.pdf'
    return p if p.exists() else None


def commit_pool_uploads(data_root: Path, pool_id: str,
                        cml_token: str, sa_token: str,
                        date_str: str) -> Dict[str, str]:
    """Move temp-uploaded CML + Schedule-A into their final per-pool slot
    under data/{date}/raw/cml_uploads/. Returns absolute paths the email
    step uses."""
    dst_dir = data_root / date_str / 'raw' / 'cml_uploads'
    dst_dir.mkdir(parents=True, exist_ok=True)
    out: Dict[str, str] = {}
    for kind, tok in (('cml', cml_token), ('schedule_a', sa_token)):
        if not tok:
            continue
        src = upload_dir(data_root) / f'{kind}_{tok}.pdf'
        if not src.exists():
            logger.warning(f'commit_pool_uploads: {kind} token {tok} missing')
            continue
        dst = dst_dir / f'{pool_id}_{kind}.pdf'
        shutil.move(str(src), str(dst))
        out[kind] = str(dst)
    return out


# ── Strategy DOCX generation ──────────────────────────────────────────── #

def _set_cell(cell, text: str, bold: bool = False):
    cell.text = ''
    p = cell.paragraphs[0]
    run = p.add_run(text or '')
    run.bold = bold


def generate_strategy_doc(pool: Dict[str, Any],
                          out_path: Path) -> Path:
    """Build the broker-onboarding strategy DOCX for ``pool``.

    Mirrors the sample at
    ``C:/Users/Administrator/Downloads/AI Portfolio Details.docx``:
    three two-column tables for client identity, depository info, bank
    info. Fields not yet captured on the pool record render as
    ``[TBD — fill before sending]`` so the operator can patch the doc
    before it goes out (or run a future sync once the fields are
    captured)."""
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    pool_name = (pool.get('display_name') or pool.get('pool_id') or '').strip()
    pool_id   = (pool.get('pool_id') or '').strip()

    # Header
    h = doc.add_paragraph()
    run = h.add_run(f'{GSW_NAME} — {pool_name}')
    run.bold = True
    run.font.size = Pt(14)
    doc.add_paragraph(
        f"Strategy onboarding details for {pool_name}. PMS pool managed "
        f"under {GSW_NAME} (PAN {GSW_PAN})."
    )
    doc.add_paragraph()

    # Table 1 — Client identity
    doc.add_paragraph().add_run('Client Identity').bold = True
    t1 = doc.add_table(rows=5, cols=2)
    t1.style = 'Table Grid'
    rows = [
        ('Client Name', f'{GSW_NAME.upper()} {pool_name.upper()}'),
        ('Client Type', 'PMS'),
        ('UCC Code',    pool.get('ucc_code')   or '[TBD — fill before sending]'),
        ('PAN',         GSW_PAN),
        ('CP Code',     pool.get('cp_code')    or '[TBD — fill before sending]'),
    ]
    for i, (k, v) in enumerate(rows):
        _set_cell(t1.rows[i].cells[0], k, bold=True)
        _set_cell(t1.rows[i].cells[1], v)
    doc.add_paragraph()

    # Table 2 — Demat (Depository) details — single row, exchange-agnostic.
    # The CML carries one CC Id + CM BP Id per pool (no separate NSE / BSE
    # values), so the table no longer splits by exchange.
    doc.add_paragraph().add_run('Demat Account Details').bold = True
    t2 = doc.add_table(rows=2, cols=5)
    t2.style = 'Table Grid'
    headers = ['Depository', 'DP ID', 'Client ID', 'CM BP ID', 'CC ID']
    for i, h in enumerate(headers):
        _set_cell(t2.rows[0].cells[i], h, bold=True)
    dp_id      = pool.get('dp_id')        or pool.get('dpid')        or ''
    dp_client  = pool.get('dp_client_id') or ''
    dp_name    = 'National Securities Depository Ltd.' if (pool.get('dp', 'NSDL').upper() in ('NSDL', 'NATIONAL SECURITIES DEPOSITORY LTD.', 'NATIONAL SECURITIES DEPOSITORY LIMITED')) else (pool.get('dp') or '[TBD]')
    _set_cell(t2.rows[1].cells[0], dp_name)
    _set_cell(t2.rows[1].cells[1], dp_id     or '[TBD]')
    _set_cell(t2.rows[1].cells[2], dp_client or '[TBD]')
    _set_cell(t2.rows[1].cells[3], pool.get('cm_bp_id') or '[TBD — fill before sending]')
    _set_cell(t2.rows[1].cells[4], pool.get('cc_id')    or '[TBD — fill before sending]')
    doc.add_paragraph()

    # Table 3 — Bank details
    doc.add_paragraph().add_run('Bank Account Details').bold = True
    t3 = doc.add_table(rows=3, cols=2)
    t3.style = 'Table Grid'
    rows = [
        ('Bank Account Number', pool.get('bank_account')   or '[TBD]'),
        ('Name of the Bank',    pool.get('bank_full_name') or '[TBD]'),
        ('Branch Address',      pool.get('bank_branch_address') or '[TBD — fill before sending]'),
    ]
    for i, (k, v) in enumerate(rows):
        _set_cell(t3.rows[i].cells[0], k, bold=True)
        _set_cell(t3.rows[i].cells[1], v)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path


# ── Email send ─────────────────────────────────────────────────────────── #

def _load_brokers(broker_map_path: Path) -> List[Dict[str, Any]]:
    if not broker_map_path.exists():
        return []
    try:
        data = json.loads(broker_map_path.read_text(encoding='utf-8'))
        return data.get('brokers', []) or []
    except Exception:
        logger.warning(f'broker_map.json unreadable at {broker_map_path}')
        return []


def send_invitations_for_pool(pool: Dict[str, Any],
                               cml_path: Optional[str],
                               broker_map_path: Path,
                               data_root: Path,
                               date_str: str,
                               email_ingestor) -> Dict[str, Any]:
    """Generate the strategy DOCX + send a New-Trading-Code email to
    every broker that has at least one ``email_to`` recipient.

    Returns a per-broker summary dict for the UI / log.
    """
    pool_name = (pool.get('display_name') or pool.get('pool_id') or '').strip()
    pool_id   = (pool.get('pool_id') or 'unknown').strip()

    # Build the strategy doc once, reuse across all brokers.
    docs_dir = data_root / date_str / 'raw' / 'cml_uploads'
    doc_path = docs_dir / f'{pool_id}_strategy.docx'
    try:
        generate_strategy_doc(pool, doc_path)
    except Exception as e:
        logger.exception('strategy doc generation failed')
        return {'ok': False, 'error': f'doc gen failed: {e}',
                'sent': [], 'skipped': [], 'failed': []}

    subject = EMAIL_SUBJECT_TEMPLATE.format(pool_name=pool_name)
    body_text = EMAIL_BODY_TEMPLATE.format(pool_name=pool_name, pan=GSW_PAN)
    body_html = '<p>' + body_text.replace('\n\n', '</p><p>').replace('\n', '<br>') + '</p>'

    attachments = [{
        'name':         f'{pool_id}_strategy.docx',
        'path':         str(doc_path),
        'content_type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    }]
    if cml_path and Path(cml_path).exists():
        attachments.append({
            'name':         f'{pool_id}_cml.pdf',
            'path':         cml_path,
            'content_type': 'application/pdf',
        })

    sent: List[Dict[str, Any]]    = []
    skipped: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]]  = []

    # Kotak Securities requires a stamped + signed 'Trading Accounts
    # Details' letter as a PDF — built natively here so we don't have
    # to depend on Word / LibreOffice on the deploy host. Generated
    # once and reused for every Kotak broker recipient (in practice
    # there's only one broker named Kotak, but the loop tolerates more).
    kotak_pdf_path: Optional[Path] = None
    try:
        from core.kotak_letter import generate as _kotak_generate
        kotak_pdf_path = docs_dir / f'{pool_id}_kotak_letter.pdf'
        _kotak_generate(pool, kotak_pdf_path)
    except Exception as e:
        logger.exception('kotak letter generation failed')
        kotak_pdf_path = None

    def _is_kotak(broker_name: str) -> bool:
        return 'kotak' in (broker_name or '').lower()

    for b in _load_brokers(broker_map_path):
        if b.get('active') is False:
            continue
        recipients = b.get('email_to') or []
        if isinstance(recipients, str):
            recipients = [r.strip() for r in recipients.split(',') if r.strip()]
        if not recipients:
            skipped.append({'broker': b.get('name', '?'), 'reason': 'no email_to'})
            continue
        # Kotak attaches the stamped PDF letter in addition to the
        # standard strategy DOCX + CML PDF. Other brokers get only
        # the standard pair so the build doesn't have to wait on the
        # ReportLab render path for them.
        broker_attachments = list(attachments)
        if _is_kotak(b.get('name', '')) and kotak_pdf_path and kotak_pdf_path.exists():
            broker_attachments.append({
                'name':         f'{pool_id}_trading_accounts_details.pdf',
                'path':         str(kotak_pdf_path),
                'content_type': 'application/pdf',
            })
        try:
            res = email_ingestor.send_simple_email(
                subject=subject,
                body_html=body_html,
                recipients=recipients,
                attachments=broker_attachments,
            )
            if res.get('ok'):
                sent.append({'broker': b.get('name', '?'), 'recipients': recipients})
            else:
                failed.append({'broker': b.get('name', '?'),
                               'recipients': recipients,
                               'error': res.get('message', 'unknown')})
        except Exception as e:
            logger.exception(f"invitation send failed for {b.get('name')}")
            failed.append({'broker': b.get('name', '?'),
                           'recipients': recipients,
                           'error': str(e)})

    return {'ok': True, 'subject': subject,
            'sent': sent, 'skipped': skipped, 'failed': failed,
            'doc_path': str(doc_path),
            'cml_path': str(cml_path) if cml_path else None}


def send_invitations_async(pool: Dict[str, Any],
                            cml_path: Optional[str],
                            broker_map_path: Path,
                            data_root: Path,
                            date_str: str,
                            email_ingestor) -> threading.Thread:
    """Fire-and-forget wrapper. Pool creation has already returned to
    the operator by the time this runs."""
    def _run():
        try:
            result = send_invitations_for_pool(
                pool, cml_path, broker_map_path,
                data_root, date_str, email_ingestor,
            )
            logger.info(
                f"Pool invitation: {len(result.get('sent', []))} sent, "
                f"{len(result.get('skipped', []))} skipped, "
                f"{len(result.get('failed', []))} failed"
            )
        except Exception as e:
            logger.exception('send_invitations_async crashed')
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t
