"""
core/client_welcome_email.py — send a welcome email to the client after
the WS-side authorize-ws step succeeds.

One email per pool the client is on. Each email carries:
  * Greeting using the holder's full name
  * A summary table (Client ID, Client Name, UCC Code, PMS, Scheme Code)
  * Bank-detail block for that specific pool (from pools_hub)
  * Login URL + the operator's chosen username + the firm-wide default
    first-login password (which the client must change on first login)
  * The client's CML PDF as an attachment (when persisted; legacy
    clients without a stored CML send without it)

Recipients
==========
  * To  — client.email
  * Cc  — Fund Manager email (looked up via PoolsHub.fund_manager_email)
  * Cc  — Intermediary email (from the Distributor entity in
          fee_entity_payments) when the client's intermediary_user_name
          isn't blank / 'DIRECT'

Configuration (env-driven so a future white-label deploy can override
without a code change):
  * KEYSTONE_LOGIN_URL              default 'https://app.thegoldstandard.in'
  * KEYSTONE_CLIENT_DEFAULT_PWD     default 'GSW@1234'

Public surface
==============
    send_welcome_emails(client, pools, *, email_ingestor) -> dict
        Returns ``{ok, sent: [...], skipped: [...], failed: [...]}``
        — one entry per pool. Caller decides how to surface in the UI.
"""
from __future__ import annotations

import logging
import os
from html import escape as _esc
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Hardcoded defaults — operator overrides via env vars when deploying
# under a different firm name / portal URL. Keystone is single-tenant
# today so the constants double as the production values.
DEFAULT_LOGIN_URL = 'https://app.thegoldstandard.in'
DEFAULT_PASSWORD  = 'GSW@1234'
PMS_FIRM_NAME     = 'GoldStandard Wealth Pvt Ltd'

EMAIL_SUBJECT_TEMPLATE = 'Welcome to {pms} — Account Details for {pool_name}'


def _login_url() -> str:
    return os.environ.get('KEYSTONE_LOGIN_URL') or DEFAULT_LOGIN_URL


def _default_password() -> str:
    return os.environ.get('KEYSTONE_CLIENT_DEFAULT_PWD') or DEFAULT_PASSWORD


def _full_name(client: Dict[str, Any]) -> str:
    parts = [(client.get(k) or '').strip()
              for k in ('first_name', 'middle_name', 'last_name')]
    return ' '.join(p for p in parts if p)


def _build_body_html(client: Dict[str, Any], pool: Dict[str, Any]) -> str:
    """Render the welcome email's HTML body for one pool. Layout matches
    the operator-supplied sample: greeting, summary table, bank block,
    login footer."""
    name        = _full_name(client) or client.get('first_name', '')
    client_id   = (client.get('dp_client_id') or '').strip()
    ucc_code    = (pool.get('ws_account_code') or '').strip()
    scheme_code = (pool.get('bank_account_holder')
                    or pool.get('display_name') or '').strip()
    pool_name   = (pool.get('display_name') or '').strip()
    short_name  = pool_name  # Keep simple — operator's sample uses display name
    user_name   = (client.get('user_name') or '').strip()

    # Bank block fields. Empty values render as a placeholder so the
    # client sees what's pending (rather than a broken-looking blank
    # row). bank_full_name has a fallback path: if the pool record
    # doesn't carry it (legacy pools that predate the Pool Creator
    # wizard), derive from the bank short code via bank_registry —
    # so 'ICICI' on the pool record renders as 'ICICI Bank Ltd.'
    # automatically. The other three (holder/IFSC/MICR) are pool-
    # specific and can't be derived; they stay [pending] until the
    # operator backfills via Edit Pool or scripts/backfill_pool_bank_fields.py.
    miss = '<i style="color:#aab">[pending]</i>'
    bank_full_raw = (pool.get('bank_full_name') or '').strip()
    if not bank_full_raw:
        try:
            from core.bank_registry import bank_full_name as _bfn
            bank_full_raw = (_bfn(pool.get('bank') or '') or '').strip()
        except Exception:
            bank_full_raw = ''
    bank_full   = _esc(bank_full_raw)                  or miss
    holder      = _esc(pool.get('bank_account_holder') or '') or miss
    acno        = _esc(pool.get('bank_account')        or '') or miss
    ifsc        = _esc(pool.get('bank_ifsc')           or '') or miss
    micr        = _esc(pool.get('bank_micr')           or '') or miss

    css_table = (
        'border-collapse:collapse;font-family:Arial,sans-serif;'
        'font-size:13px;margin:12px 0'
    )
    css_th = (
        'background:#0b1929;color:#fff;padding:8px 12px;text-align:left;'
        'font-weight:600;border:1px solid #0b1929;font-size:12px'
    )
    css_td = (
        'padding:8px 12px;border:1px solid #d8dde3;font-size:12px'
    )

    summary_row = (
        f'<tr>'
        f'<td style="{css_td}">{_esc(client_id) or miss}</td>'
        f'<td style="{css_td}">{_esc(name)}</td>'
        f'<td style="{css_td}">{_esc(ucc_code) or miss}</td>'
        f'<td style="{css_td}">{_esc(PMS_FIRM_NAME)}</td>'
        f'<td style="{css_td}">{_esc(scheme_code)}</td>'
        f'</tr>'
    )

    return f"""
<div style="font-family:Arial,sans-serif;font-size:14px;color:#222;line-height:1.55">
  <p>Dear {_esc(name)},</p>

  <p>Please find attached the CML copy of the demat account.</p>

  <table style="{css_table}">
    <thead>
      <tr>
        <th style="{css_th}">CLIENT ID</th>
        <th style="{css_th}">CLIENT NAME</th>
        <th style="{css_th}">UCC Code / Client Code</th>
        <th style="{css_th}">PMS</th>
        <th style="{css_th}">Scheme Code</th>
      </tr>
    </thead>
    <tbody>{summary_row}</tbody>
  </table>

  <p>Additionally, please find the bank details below in case you wish to
     invest in the {_esc(pool_name)} through a cash contribution:</p>

  <p style="font-weight:600;margin:14px 0 6px">{_esc(pool_name)}</p>
  <table style="{css_table}">
    <tbody>
      <tr><td style="{css_td};font-weight:600">Bank Name</td><td style="{css_td}">{bank_full}</td></tr>
      <tr><td style="{css_td};font-weight:600">Account Holder Name</td><td style="{css_td}">{holder}</td></tr>
      <tr><td style="{css_td};font-weight:600">Bank Account Number</td><td style="{css_td}">{acno}</td></tr>
      <tr><td style="{css_td};font-weight:600">IFSC Code</td><td style="{css_td}">{ifsc}</td></tr>
      <tr><td style="{css_td};font-weight:600">MICR Code</td><td style="{css_td}">{micr}</td></tr>
    </tbody>
  </table>

  <p style="margin-top:18px">
    To track your portfolio, please log in at
    <a href="{_esc(_login_url())}" style="color:#0b1929">{_esc(_login_url())}</a>.
  </p>
  <table style="{css_table}">
    <tbody>
      <tr><td style="{css_td};font-weight:600;width:160px">Username</td><td style="{css_td}"><code>{_esc(user_name) or miss}</code></td></tr>
      <tr><td style="{css_td};font-weight:600">Default Password</td><td style="{css_td}"><code>{_esc(_default_password())}</code></td></tr>
    </tbody>
  </table>
  <p style="color:#a82;font-size:12px"><strong>Important:</strong> please change your password on first login.</p>

  <p style="margin-top:24px">Regards,<br>{_esc(PMS_FIRM_NAME)}</p>
</div>
""".strip()


def _resolve_recipients(client: Dict[str, Any],
                         pool: Dict[str, Any]) -> Dict[str, List[str]]:
    """Build To/Cc lists.

    To = client.
    Cc = every named entity on this pool's fee breakup (FM, Distributor,
         Residual), looked up by name in fee_entity_payments — that's
         the canonical store with the Email column on the Settings →
         Fee Entities table. The current/latest breakup_period is the
         one that matters; older periods reflect previous arrangements
         we don't need to Cc.

    Falls back to legacy lookups when no breakup_periods is configured
    yet (pre-time-varying-fees clients): pool.fund_manager via pools_hub
    fund_manager_emails, plus client.intermediary_user_name via
    fee_entity_payments. Either way, blank lookups drop silently —
    the email still sends to whatever's populated.
    """
    to: List[str] = []
    cc: List[str] = []

    client_email = (client.get('email') or '').strip()
    if client_email:
        to.append(client_email)

    # Helper: case-insensitive Cc dedup.
    seen_cc = set()
    def _add_cc(addr: Optional[str]) -> None:
        a = (addr or '').strip()
        if not a:
            return
        key = a.lower()
        if key in seen_cc:
            return
        seen_cc.add(key)
        cc.append(a)

    # Helper: skip the synthetic GSW name (the firm itself never goes
    # on Cc — the From address IS the firm) and the 'DIRECT' /
    # 'GSWDIRECT' sentinels (which mean 'no intermediary').
    def _is_skippable_name(name: str) -> bool:
        u = (name or '').strip().upper()
        return u in ('', 'DIRECT', 'GSWDIRECT', 'GOLDSTANDARD WEALTH',
                     'GOLDSTANDARD WEALTH PVT LTD')

    # ── Primary path: pull every named earner from the fee breakup ─── #
    fc = pool.get('fee_config') or {}
    breakups = fc.get('breakup_periods')
    # Tolerate the legacy flat-shape stored on older client rows: wrap
    # into a single pseudo-period so the same lookup logic runs.
    if not isinstance(breakups, list) or not breakups:
        legacy_keys = ('share_gsw_pct', 'share_fm_pct', 'share_distributor_pct',
                       'share_residual_pct', 'fm_user_name',
                       'distributor_user_name', 'residual_user_name')
        if any(k in fc for k in legacy_keys):
            breakups = [fc]
        else:
            breakups = []

    breakup_used = False
    if breakups:
        # Latest period = the entry with the largest effective_from
        # string (ISO YYYY-MM-DD sorts lexicographically). Empty
        # effective_from sorts before any real date — fine for the
        # legacy single-entry case.
        latest = sorted(breakups,
                        key=lambda b: (b.get('effective_from') or ''))[-1]
        try:
            from core.fee_entity_payments import get_entity_email
        except Exception as e:
            logger.warning(f'fee_entity_payments import failed: {e}')
            get_entity_email = lambda _n: ''
        for role_field in ('fm_user_name', 'distributor_user_name',
                            'residual_user_name'):
            name = (latest.get(role_field) or '').strip()
            if not name or _is_skippable_name(name):
                continue
            try:
                _add_cc(get_entity_email(name))
                breakup_used = True
            except Exception as e:
                logger.warning(f'entity email lookup failed for {name!r}: {e}')

    # ── Fallback: legacy lookups when no breakup is configured ─────── #
    # Runs only when the breakup-based path produced nothing — usually
    # an older client whose fee_config was never migrated. Pool-level
    # FM (from pools_hub schemes) + client-level intermediary cover
    # the historical setup. Each _add_cc dedups, so pools_hub falling
    # through to a value already added via fee_entity_payments is fine.
    if not breakup_used:
        fm_name = (pool.get('fund_manager') or '').strip()
        if fm_name and not _is_skippable_name(fm_name):
            try:
                from core.fee_entity_payments import get_entity_email
                _add_cc(get_entity_email(fm_name))
            except Exception:
                pass
            try:
                from core.pools_hub import PoolsHub
                _add_cc(PoolsHub.load().fund_manager_email(fm_name))
            except Exception as e:
                logger.warning(f'FM email pools_hub lookup failed: {e}')

        interm = (client.get('intermediary_user_name') or '').strip()
        if interm and not _is_skippable_name(interm):
            try:
                from core.fee_entity_payments import get_entity_email
                _add_cc(get_entity_email(interm))
            except Exception as e:
                logger.warning(f'Intermediary email lookup failed for {interm!r}: {e}')

    return {'to': to, 'cc': cc}


def send_welcome_emails(client: Dict[str, Any],
                         pools: List[Dict[str, Any]],
                         *,
                         email_ingestor) -> Dict[str, Any]:
    """Fire one welcome email per pool. ``pools`` is the resolved list
    from :func:`_pools_for_client` — each entry must already carry the
    bank fields and the fund_manager name from pools_hub.

    Returns ``{ok, sent: [...], skipped: [...], failed: [...]}`` so the
    caller can surface a summary on the UI / log."""
    from core.client_cml_storage import cml_path

    sent:    List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    failed:  List[Dict[str, Any]] = []

    name = _full_name(client) or client.get('first_name', '')

    # CML attachment lives once per client — same file goes to every
    # pool's email since the demat account is shared across pools.
    row_id = client.get('id')
    cml = cml_path(int(row_id)) if row_id else None
    attachments: List[Dict[str, Any]] = []
    if cml and cml.exists():
        attachments.append({
            'name':         f'CML_{(client.get("dp_client_id") or "client")}.pdf',
            'path':         str(cml),
            'content_type': 'application/pdf',
        })

    for pool in pools:
        pool_name = (pool.get('display_name') or pool.get('pool_id') or '').strip()
        recipients = _resolve_recipients(client, pool)
        if not recipients['to']:
            skipped.append({
                'pool':   pool_name,
                'reason': 'client email blank',
            })
            continue
        subject = EMAIL_SUBJECT_TEMPLATE.format(
            pms=PMS_FIRM_NAME, pool_name=pool_name,
        )
        body_html = _build_body_html(client, pool)
        try:
            res = email_ingestor.send_simple_email(
                subject=subject,
                body_html=body_html,
                recipients=recipients['to'],
                cc=recipients['cc'] or None,
                attachments=attachments,
            )
            if res.get('ok'):
                sent.append({
                    'pool':       pool_name,
                    'to':         recipients['to'],
                    'cc':         recipients['cc'],
                    'cml':        bool(attachments),
                })
            else:
                failed.append({
                    'pool':   pool_name,
                    'to':     recipients['to'],
                    'cc':     recipients['cc'],
                    'error':  res.get('message', 'unknown'),
                })
        except Exception as e:
            logger.exception(f'welcome email send failed for {name} / {pool_name}')
            failed.append({
                'pool':   pool_name,
                'to':     recipients['to'],
                'cc':     recipients['cc'],
                'error':  str(e),
            })

    return {'ok': True, 'sent': sent, 'skipped': skipped, 'failed': failed}


def pools_for_client(client: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the list of pools (with bank fields + fund_manager) the
    client is on. Primary pool first (top-level columns), then any
    entries in the ``additional_pools`` JSON column.

    Pool data is enriched from pools_hub by looking up the pool_mapin /
    ws_account_code / scheme_name — operator may have entered any of
    those into the client form, so we try each in turn."""
    import json as _json
    try:
        from core.pools_hub import PoolsHub
        hub = PoolsHub.load()
    except Exception as e:
        logger.warning(f'PoolsHub load failed for welcome email: {e}')
        return []

    # Helper: parse a fee_config dict from a stored value (may be a JSON
    # string, an already-decoded dict, or null/blank). Welcome email Cc
    # resolution reads breakup_periods off this — see _resolve_recipients.
    def _parse_fc(raw):
        if raw is None or raw == '':
            return None
        if isinstance(raw, dict):
            return raw
        try:
            d = _json.loads(raw)
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    # Build a primary-pool descriptor from top-level columns.
    raw_pools: List[Dict[str, Any]] = []
    primary = {
        'ws_account_code': (client.get('ws_account_code') or '').strip(),
        'pool_mapin':      (client.get('pool_mapin') or '').strip(),
        'scheme_name':     (client.get('scheme_name') or '').strip(),
        'fee_config':      _parse_fc(client.get('fee_config')),
    }
    if any(v for k, v in primary.items() if k != 'fee_config'):
        raw_pools.append(primary)
    try:
        extras = _json.loads(client.get('additional_pools') or '[]') or []
        for p in extras:
            if isinstance(p, dict) and any(v for v in p.values()):
                raw_pools.append({
                    'ws_account_code': (p.get('ws_account_code') or '').strip(),
                    'pool_mapin':      (p.get('pool_mapin') or '').strip(),
                    'scheme_name':     (p.get('scheme_name') or '').strip(),
                    'fee_config':      _parse_fc(p.get('fee_config')),
                })
    except Exception:
        pass

    # Resolve each (mapin / scheme_name) to the pools_hub record so the
    # email body has bank fields + fund_manager.
    resolved: List[Dict[str, Any]] = []
    for rp in raw_pools:
        match: Optional[Dict[str, Any]] = None
        # Try mapin first — most reliable.
        if rp['pool_mapin']:
            for p in hub.pools:
                if (p.get('mapin') or '').strip() == rp['pool_mapin']:
                    match = p
                    break
        # Fall back to scheme_name.
        if match is None and rp['scheme_name']:
            sn = rp['scheme_name'].lower().strip()
            for p in hub.pools:
                names = [(n or '').lower().strip()
                          for n in (p.get('ws_scheme_names') or [])]
                if sn in names or sn == (p.get('display_name') or '').lower().strip():
                    match = p
                    break
        if match is None:
            # No pools_hub match — emit a degraded row so the operator
            # at least sees something rather than a silent skip. Banks
            # render as [pending] in the body. fee_config still flows
            # through so the Cc list is correct even without bank info.
            resolved.append({
                'pool_id':              '',
                'display_name':         rp['scheme_name'] or rp['pool_mapin'] or '',
                'ws_account_code':      rp['ws_account_code'],
                'fund_manager':         '',
                'fee_config':           rp.get('fee_config'),
            })
            continue
        # Merge pools_hub + the per-pool ws_account_code (from client
        # record's additional_pools — pools_hub itself doesn't carry it
        # since one pool can serve multiple clients).
        merged = dict(match)
        merged['ws_account_code'] = rp['ws_account_code'] or merged.get('ws_account_code', '')
        # Carry the operator's fee_config through so _resolve_recipients
        # can read breakup_periods and Cc the FM / Distributor / Residual.
        merged['fee_config'] = rp.get('fee_config')
        # Look up the linked scheme to pull the fund_manager name.
        try:
            sn = (merged.get('ws_scheme_names') or [''])[0]
            for sc in hub._schemes:
                if (sc.get('scheme_name') or '').lower().strip() == sn.lower().strip():
                    merged['fund_manager'] = sc.get('fund_manager') or merged.get('fund_manager', '')
                    break
        except Exception:
            pass
        resolved.append(merged)

    return resolved
