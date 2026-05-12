"""
Entity fixed payments
=====================

Per-entity monthly fixed payment (a salary, retainer, or other fixed
cost outside the variable share). Used by the fee calculator to
report Net = Gross − Fixed for each fee earner.

Storage: JSON file at data/fee_entity_payments.json
Shape:   {"name": {"monthly_fixed": float|null, "start_date": ISO|null,
                    "entity_type": "Fund Manager"|"Advisor"|"Distributor"|
                                    "Tech Partner"|"Residual"|null}}

A blank/None monthly_fixed means "no fixed payment configured" — the
entity is in the roster but has no fixed line for the period. Net
then equals Gross.

entity_type classifies the entity for downstream consumers:
- Advisor and Distributor populate the operator-facing dropdowns on
  the client creation form (advisor_user_name and intermediary_user_name
  fields, respectively, since WS treats Intermediary and Distributor
  as the same column).
- Fund Manager / Residual / Tech Partner are reported categories on the
  Earnings by entity rollup.

Period conversion convention: monthly × period_days / 30. 30 is the
clean divisor (one month = 30 days). For exact calendar accuracy a
caller could substitute 30.4167 (= 365 / 12) but the operator can
refine later if it matters.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Allowed entity_type values. Anything else is rejected at write time.
# Keeping this as a tuple (not a set) preserves the canonical display
# order callers can iterate when populating the operator dropdown.
ENTITY_TYPES = (
    'Fund Manager',
    'Advisor',
    'Distributor',
    'Tech Partner',
    'Residual',
)


def _path() -> Path:
    """data/fee_entity_payments.json under APP_DIR (or KEYSTONE_DATA_DIR
    if set, mirroring the rest of the data tree)."""
    base = os.environ.get('KEYSTONE_DATA_DIR') or str(Path(__file__).parent.parent)
    p = Path(base) / 'data' / 'fee_entity_payments.json'
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_payments() -> Dict[str, Dict[str, Any]]:
    """Return {name: {monthly_fixed, start_date}} from the JSON store.

    Backward-compatible with the legacy shape {name: float|null} that
    the original v1 store wrote — any number/null at the top level is
    wrapped into the new dict shape (start_date defaults to None).
    """
    p = _path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding='utf-8') or '{}')
    except Exception as e:
        logger.warning(f'fee_entity_payments parse failed: {e}')
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not k.strip():
            continue
        nm = k.strip()
        if v is None or v == '':
            out[nm] = {'monthly_fixed': None, 'start_date': None,
                       'entity_type': None, 'email': ''}
        elif isinstance(v, (int, float)):
            # Legacy shape: bare number → wrap.
            out[nm] = {'monthly_fixed': float(v), 'start_date': None,
                       'entity_type': None, 'email': ''}
        elif isinstance(v, dict):
            mf = v.get('monthly_fixed')
            sd = v.get('start_date')
            et = v.get('entity_type')
            em = v.get('email')
            try:
                mf_norm: Optional[float] = (None if mf is None or mf == ''
                                            else float(mf))
            except (TypeError, ValueError):
                mf_norm = None
            sd_norm: Optional[str] = (sd.strip()
                                      if isinstance(sd, str) and sd.strip()
                                      else None)
            # entity_type must be one of the canonical types or it's dropped.
            # Anything we don't recognise is silently normalised to None so
            # an old or hand-edited file doesn't produce a dropdown with a
            # value the operator can't reselect.
            et_norm: Optional[str] = (et if isinstance(et, str) and et in ENTITY_TYPES
                                      else None)
            em_norm: str = em.strip() if isinstance(em, str) else ''
            out[nm] = {'monthly_fixed': mf_norm, 'start_date': sd_norm,
                       'entity_type': et_norm, 'email': em_norm}
        else:
            out[nm] = {'monthly_fixed': None, 'start_date': None,
                       'entity_type': None, 'email': ''}
    return out


def save_payments(d: Dict[str, Dict[str, Any]]) -> None:
    """Replace the store with the given dict. Atomic write via tempfile +
    rename so a partial write can't corrupt the JSON."""
    p = _path()
    tmp = p.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(d, indent=2, sort_keys=True), encoding='utf-8')
    tmp.replace(p)


def set_payment(name: str, monthly_fixed: Optional[Any] = None,
                start_date: Optional[str] = None,
                entity_type: Optional[Any] = None,
                email: Optional[Any] = None) -> Dict[str, Any]:
    """Set one entity's fixed payment + start date + type + email.
    Fields are replaced atomically — pass null/blank for any to clear
    it. The entity stays in the store regardless. start_date must
    parse as YYYY-MM-DD when non-blank. entity_type, when non-blank,
    must be one of ENTITY_TYPES. email, when non-blank, is a free-form
    string (no validation — operator's responsibility)."""
    nm = (name or '').strip()
    if not nm:
        return {'ok': False, 'error': 'name required'}
    # monthly_fixed: blank/null → null (no fixed payment), number → set.
    if monthly_fixed in (None, ''):
        mf_new: Optional[float] = None
    else:
        try:
            mf_new = float(monthly_fixed)
        except (TypeError, ValueError):
            return {'ok': False, 'error': f'invalid amount: {monthly_fixed!r}'}
    # start_date: blank/null → null. Otherwise must parse ISO.
    if start_date in (None, ''):
        sd_new: Optional[str] = None
    elif isinstance(start_date, str):
        from datetime import datetime as _dt
        try:
            _dt.strptime(start_date, '%Y-%m-%d')
            sd_new = start_date
        except ValueError:
            return {'ok': False, 'error': f'start_date must be YYYY-MM-DD: {start_date!r}'}
    else:
        sd_new = None
    # entity_type: blank/null → null. Otherwise must be a canonical type.
    if entity_type in (None, ''):
        et_new: Optional[str] = None
    elif isinstance(entity_type, str) and entity_type in ENTITY_TYPES:
        et_new = entity_type
    else:
        return {'ok': False,
                'error': f'entity_type must be one of {ENTITY_TYPES}: {entity_type!r}'}
    # email: blank/null → ''. No validation — operator's responsibility.
    em_new: str = (email or '').strip() if isinstance(email, str) else ''
    payments = load_payments()
    payments[nm] = {'monthly_fixed': mf_new, 'start_date': sd_new,
                    'entity_type': et_new, 'email': em_new}
    save_payments(payments)
    return {'ok': True, 'name': nm, **payments[nm]}


def remove_entity(name: str) -> Dict[str, Any]:
    """Remove an entity from the store entirely (operator deleted)."""
    nm = (name or '').strip()
    if not nm:
        return {'ok': False, 'error': 'name required'}
    payments = load_payments()
    if nm in payments:
        del payments[nm]
        save_payments(payments)
        return {'ok': True, 'removed': nm}
    return {'ok': True, 'removed': None,
            'note': f'{nm} was not in the store'}


def list_entities_with_roster_union() -> Dict[str, Any]:
    """Roster of every entity name we know about (from existing
    fee_configs) UNION every name in the payments store, sorted
    alphabetically. Each entry carries the monthly_fixed value if set,
    otherwise None.

    The roster comes from the same /api/fees/roster source the modal
    datalist uses, so adding an entity here doesn't fork it from the
    rest of the system.
    """
    from .fee_rates import list_roster_names
    payments = load_payments()
    roster = set(list_roster_names())
    all_names = sorted(roster | set(payments.keys()), key=lambda s: s.lower())
    return {
        'entities': [
            {
                'name': n,
                'monthly_fixed': (payments.get(n) or {}).get('monthly_fixed'),
                'start_date':    (payments.get(n) or {}).get('start_date'),
                'entity_type':   (payments.get(n) or {}).get('entity_type'),
                'email':         (payments.get(n) or {}).get('email', ''),
                'in_roster':     n in roster,
                'has_payment':   n in payments,
            }
            for n in all_names
        ],
        'types': list(ENTITY_TYPES),
    }


def get_entity_email(name: str) -> str:
    """Look up an entity's email by name (case-insensitive). Returns
    '' when no email is configured. Used by the welcome-email send path
    to resolve the intermediary's Cc address."""
    if not name:
        return ''
    target = name.strip().lower()
    for k, v in load_payments().items():
        if k.strip().lower() == target:
            return ((v or {}).get('email') or '').strip()
    return ''


def list_entities_by_type(entity_type: str) -> List[str]:
    """Names of every entity tagged with the given type, sorted
    case-insensitively. Used by the client creation form to populate
    the Advisor / Intermediary dropdowns. Returns an empty list when
    the type is unrecognised or no entities match yet — callers should
    fall back to a free-text input or to the prefilled hardcoded value
    so the form never blocks waiting on classification."""
    if entity_type not in ENTITY_TYPES:
        return []
    payments = load_payments()
    return sorted(
        (n for n, v in payments.items() if (v or {}).get('entity_type') == entity_type),
        key=lambda s: s.lower(),
    )
