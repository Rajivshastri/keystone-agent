"""
Fees: Excel export
==================

Bundles every dataset visible on the Fees screen into a single
.xlsx file:

  Sheet 1 — Summary:           period, totals by share kind,
                               grand totals + weighted headline rate.
  Sheet 2 — Earnings by entity: parent + child rows, totals row,
                               weighted-rate column.
  Sheet 3 — Per account:        every line item (AUM-having + zero-AUM)
                               with role names, fees per share kind.
  Sheet 4 — Fee rates by account: every PAN's pools with current
                                  configured rates and role names.
  Sheet 5 — Entity fixed payments: monthly fixed + start_date.

Indian-locale number format (#,##,##0.00) on currency columns;
percent format with 4 decimals on rate columns. Bold + filled
header row on every sheet.
"""

from __future__ import annotations

import io
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Indian lakhs/crores grouping. Excel renders this as
# "12,34,56,789.00" rather than "123,456,789.00".
INR_FMT = '#,##,##0.00'
PCT_FMT = '0.0000'   # rates are already % values; show with 4 dp


def _set_header(ws, row: int, headers: List[str]):
    """Write a header row with bold + grey fill."""
    from openpyxl.styles import Font, PatternFill, Alignment
    fill = PatternFill('solid', fgColor='1A2E48')   # navy-light
    font = Font(bold=True, color='FFFFFF', size=11)
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=c, value=h)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal='center', vertical='center')
    ws.row_dimensions[row].height = 22


def _autosize(ws, columns: int, min_width: int = 12):
    """Best-effort column auto-fit. openpyxl can't measure rendered
    width, but we can scan max content length and pad."""
    for col_idx in range(1, columns + 1):
        col_letter = ws.cell(row=1, column=col_idx).column_letter
        max_len = min_width
        for row in ws.iter_rows(min_col=col_idx, max_col=col_idx, values_only=True):
            v = row[0]
            if v is None:
                continue
            l = len(str(v))
            if l > max_len:
                max_len = l
        ws.column_dimensions[col_letter].width = min(max_len + 2, 50)


def _write_summary(wb, result: Dict[str, Any]):
    ws = wb.create_sheet('Summary', 0)
    period = result.get('period') or {}
    totals = result.get('totals') or {}
    by_role = result.get('by_role') or {}

    ws['A1'] = 'Period'
    ws['B1'] = f"{period.get('from','')} → {period.get('to','')}"
    ws['A2'] = 'Days'
    ws['B2'] = period.get('days')
    ws['A3'] = 'AUM file'
    ws['B3'] = result.get('aum_path', '')

    _set_header(ws, 5, ['Totals by share kind', 'Amount (₹)'])
    rows = [
        ('GoldStandard Wealth (GSW share)', by_role.get('gsw',         0)),
        ('Fund Manager total',              by_role.get('fm',          0)),
        ('Distributor total',               by_role.get('distributor', 0)),
        ('Residual total',                  by_role.get('residual',    0)),
        ('Grand total (= investor fee)',    totals.get('total_fees',   0)),
    ]
    for r_idx, (lbl, val) in enumerate(rows, start=6):
        ws.cell(row=r_idx, column=1, value=lbl)
        c = ws.cell(row=r_idx, column=2, value=val)
        c.number_format = INR_FMT

    _set_header(ws, 13, ['Aggregate', 'Value'])
    aggregates = [
        ('Total accounts',                 totals.get('accounts')),
        ('Accounts with AUM',              totals.get('accounts_with_aum')),
        ('Total AUM (₹)',                  totals.get('total_aum')),
        ('Weighted headline rate (% p.a.)',totals.get('weighted_headline_pct')),
        ('Entity gross (₹)',               totals.get('entity_gross')),
        ('Entity fixed (₹)',               totals.get('entity_fixed')),
        ('Entity net (₹)',                 totals.get('entity_net')),
    ]
    for r_idx, (lbl, val) in enumerate(aggregates, start=14):
        ws.cell(row=r_idx, column=1, value=lbl)
        c = ws.cell(row=r_idx, column=2, value=val)
        if 'rate' in lbl.lower():
            c.number_format = PCT_FMT
        elif '₹' in lbl:
            c.number_format = INR_FMT
    _autosize(ws, 2, min_width=20)


def _write_by_entity(wb, result: Dict[str, Any]):
    ws = wb.create_sheet('Earnings by entity')
    headers = ['Entity', 'Role', 'Accounts', 'Gross (₹)', 'Fixed (₹)',
               'Net (₹)', 'Weighted rate (% p.a.)']
    _set_header(ws, 1, headers)

    by_earner = result.get('by_earner') or []
    # Group by entity name to render parent + child rows the same way
    # the UI does. Single-role entities just become one row.
    groups: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for e in by_earner:
        nm = e['name']
        if nm not in groups:
            groups[nm] = []
            order.append(nm)
        groups[nm].append(e)

    r = 2
    for nm in order:
        rows = groups[nm]
        is_multi = len(rows) > 1
        if is_multi:
            parent_inr = sum(x['total_inr']  for x in rows)
            parent_aum = sum(x['total_aum']  for x in rows)
            parent_acc = sum(x['accounts']   for x in rows)
            parent_axp = sum(x.get('aum_x_pct', 0) for x in rows)
            entFixed   = rows[0].get('fixed_inr')
            entNet     = parent_inr - (entFixed or 0)
            wrate      = (parent_axp / parent_aum) if parent_aum else None
            ws.cell(row=r, column=1, value=nm)
            ws.cell(row=r, column=2, value='')
            ws.cell(row=r, column=3, value=parent_acc)
            ws.cell(row=r, column=4, value=parent_inr).number_format = INR_FMT
            ws.cell(row=r, column=5, value=entFixed).number_format = INR_FMT
            ws.cell(row=r, column=6, value=entNet).number_format = INR_FMT
            if wrate is not None:
                ws.cell(row=r, column=7, value=wrate).number_format = PCT_FMT
            # Bold the parent row.
            from openpyxl.styles import Font
            for col in range(1, 8):
                ws.cell(row=r, column=col).font = Font(bold=True)
            r += 1
            for child in rows:
                ws.cell(row=r, column=1, value='  ' + nm)
                ws.cell(row=r, column=2, value=child.get('role', ''))
                ws.cell(row=r, column=3, value=child.get('accounts'))
                ws.cell(row=r, column=4, value=child.get('total_inr')).number_format = INR_FMT
                ws.cell(row=r, column=5, value=None)  # fixed only on parent
                ws.cell(row=r, column=6, value=None)  # net only on parent
                wr = child.get('weighted_rate_pct')
                if wr is not None:
                    ws.cell(row=r, column=7, value=wr).number_format = PCT_FMT
                r += 1
        else:
            child = rows[0]
            entFixed = child.get('fixed_inr')
            entNet   = child.get('total_inr') - (entFixed or 0)
            ws.cell(row=r, column=1, value=nm)
            ws.cell(row=r, column=2, value=child.get('role', ''))
            ws.cell(row=r, column=3, value=child.get('accounts'))
            ws.cell(row=r, column=4, value=child.get('total_inr')).number_format = INR_FMT
            ws.cell(row=r, column=5, value=entFixed).number_format = INR_FMT
            ws.cell(row=r, column=6, value=entNet).number_format = INR_FMT
            wr = child.get('weighted_rate_pct')
            if wr is not None:
                ws.cell(row=r, column=7, value=wr).number_format = PCT_FMT
            r += 1

    # Totals row.
    totals = result.get('totals') or {}
    ws.cell(row=r, column=1, value='Total')
    ws.cell(row=r, column=3, value=totals.get('accounts'))
    ws.cell(row=r, column=4, value=totals.get('total_fees')).number_format = INR_FMT
    ws.cell(row=r, column=5, value=totals.get('entity_fixed')).number_format = INR_FMT
    ws.cell(row=r, column=6, value=totals.get('entity_net')).number_format = INR_FMT
    rate = totals.get('weighted_headline_pct')
    if rate is not None:
        ws.cell(row=r, column=7, value=rate).number_format = PCT_FMT
    from openpyxl.styles import Font
    for col in range(1, 8):
        ws.cell(row=r, column=col).font = Font(bold=True, color='C9A84C')

    _autosize(ws, len(headers))


def _write_per_account(wb, result: Dict[str, Any]):
    ws = wb.create_sheet('Per account')
    # From / To columns reflect each row's effective AUM-billing window.
    # Mid-period account opens / closes mean these can differ from the
    # operator's selected window — surfacing the per-row span lets the
    # operator audit any outlier days without re-opening the AUM file.
    headers = ['Account', 'Investor', 'Scheme',
               'From date', 'To date', 'Days',
               'Avg AUM (₹)', 'Investor fee (₹)',
               'GSW (₹)', 'FM (₹)', 'FM name',
               'Distributor (₹)', 'Distributor name',
               'Residual (₹)', 'Residual name']
    _set_header(ws, 1, headers)
    for r_idx, a in enumerate((result.get('by_account') or []), start=2):
        ws.cell(row=r_idx, column=1,  value=a.get('account_code'))
        ws.cell(row=r_idx, column=2,  value=a.get('client_name'))
        ws.cell(row=r_idx, column=3,  value=a.get('scheme_name'))
        ws.cell(row=r_idx, column=4,  value=a.get('period_from'))
        ws.cell(row=r_idx, column=5,  value=a.get('period_to'))
        ws.cell(row=r_idx, column=6,  value=a.get('period_days'))
        ws.cell(row=r_idx, column=7,  value=a.get('avg_aum')).number_format      = INR_FMT
        ws.cell(row=r_idx, column=8,  value=a.get('investor_fee')).number_format = INR_FMT
        ws.cell(row=r_idx, column=9,  value=a.get('gsw_inr')).number_format      = INR_FMT
        ws.cell(row=r_idx, column=10, value=a.get('fm_inr')).number_format       = INR_FMT
        ws.cell(row=r_idx, column=11, value=a.get('fm_name'))
        ws.cell(row=r_idx, column=12, value=a.get('distributor_inr')).number_format = INR_FMT
        ws.cell(row=r_idx, column=13, value=a.get('distributor_name'))
        ws.cell(row=r_idx, column=14, value=a.get('residual_inr')).number_format = INR_FMT
        ws.cell(row=r_idx, column=15, value=a.get('residual_name'))
    _autosize(ws, len(headers))


def _write_fee_rates(wb):
    """Sheet 4: every pool's current fee_config (regardless of period).
    Pulls live from list_investors_with_pools so the export always
    reflects the moment the operator hit Download."""
    from .fee_rates import list_investors_with_pools
    ws = wb.create_sheet('Fee rates by account')
    headers = ['PAN', 'First name', 'Middle name', 'Last name',
               'Account', 'Scheme', 'Pool MAPIN',
               'Headline % p.a.', 'GSW %', 'FM %',
               'Distributor %', 'Residual %',
               'FM name', 'Distributor name', 'Residual name']
    _set_header(ws, 1, headers)
    r = 2
    for inv in list_investors_with_pools():
        for p in inv.get('pools', []):
            fc = p.get('fee_config') or {}
            cbg = p.get('cbg_timeline') or []
            # Headline now comes from CBG master FLATFEE (latest entry).
            # Falls back to legacy flat fc.headline_fee_pct so this sheet
            # keeps populating during the migration window.
            head = (cbg[-1].get('flatfee_pct') if cbg else None)
            if head is None:
                head = fc.get('headline_fee_pct')
            # Share breakup comes from the LATEST breakup_period (the
            # current effective config). Old flat shape: read from top
            # level. Falls back to None when nothing's configured.
            bp = (fc.get('breakup_periods') or [])
            latest_br = bp[-1] if bp else fc
            ws.cell(row=r, column=1,  value=inv.get('tax_pan'))
            ws.cell(row=r, column=2,  value=inv.get('first_name'))
            ws.cell(row=r, column=3,  value=inv.get('middle_name'))
            ws.cell(row=r, column=4,  value=inv.get('last_name'))
            ws.cell(row=r, column=5,  value=p.get('ws_account_code'))
            ws.cell(row=r, column=6,  value=p.get('scheme_name'))
            ws.cell(row=r, column=7,  value=p.get('pool_mapin'))
            c = ws.cell(row=r, column=8, value=head)
            if head is not None:
                c.number_format = PCT_FMT
            for col_offset, key in enumerate(
                ['share_gsw_pct', 'share_fm_pct',
                 'share_distributor_pct', 'share_residual_pct']
            ):
                v = (latest_br or {}).get(key)
                c = ws.cell(row=r, column=9 + col_offset, value=v)
                if v is not None:
                    c.number_format = PCT_FMT
            ws.cell(row=r, column=13, value=(latest_br or {}).get('fm_user_name'))
            ws.cell(row=r, column=14, value=(latest_br or {}).get('distributor_user_name'))
            ws.cell(row=r, column=15, value=(latest_br or {}).get('residual_user_name'))
            r += 1
    _autosize(ws, len(headers))


def _write_entity_payments(wb):
    """Sheet 5: Entity Fixed Payments table (roster ∪ store)."""
    from .fee_entity_payments import list_entities_with_roster_union
    ws = wb.create_sheet('Entity fixed payments')
    headers = ['Entity', 'Monthly fixed (₹)', 'Start date',
               'In roster', 'Has saved payment']
    _set_header(ws, 1, headers)
    data = list_entities_with_roster_union().get('entities', [])
    for r_idx, e in enumerate(data, start=2):
        ws.cell(row=r_idx, column=1, value=e.get('name'))
        v = e.get('monthly_fixed')
        c = ws.cell(row=r_idx, column=2, value=v)
        if v is not None:
            c.number_format = INR_FMT
        ws.cell(row=r_idx, column=3, value=e.get('start_date'))
        ws.cell(row=r_idx, column=4, value='Yes' if e.get('in_roster')   else 'No')
        ws.cell(row=r_idx, column=5, value='Yes' if e.get('has_payment') else 'No')
    _autosize(ws, len(headers))


def build_entity_breakdown_workbook(entity_name: str,
                                     result: Dict[str, Any]) -> bytes:
    """Single-sheet xlsx with the client-wise breakdown that rolled up
    to one entity's total on the Earnings-by-entity table.

    Source data: ``core.fee_entity_pdf.build_view`` slices the compute
    result for this entity into a list of role sections, each with
    per-pool rows. We flatten those into one sheet with a Role column
    so the operator can sort / filter freely. Sub-totals per role and
    a grand-total row at the bottom mirror the entity's rollup row.

    Columns: Role, Account, Investor, Scheme, Period from, Period to,
             Days, Avg AUM (Rs), Share %, Earnings (Rs).
    """
    from openpyxl import Workbook
    from core.fee_entity_pdf import build_view

    view = build_view(entity_name, result)
    period = result.get('period') or {}

    wb = Workbook()
    ws = wb.active
    ws.title = 'Breakdown'

    # Top metadata block.
    ws['A1'] = 'Entity'
    ws['B1'] = entity_name
    ws['A2'] = 'Period'
    ws['B2'] = f"{period.get('from','')} → {period.get('to','')}"
    ws['A3'] = 'Gross earnings (Rs)'
    c = ws.cell(row=3, column=2, value=view.get('gross_inr', 0))
    c.number_format = INR_FMT
    ws['A4'] = 'Fixed payment (Rs)'
    fixed = view.get('fixed_inr')
    c = ws.cell(row=4, column=2, value=fixed if fixed is not None else 0)
    c.number_format = INR_FMT
    ws['A5'] = 'Net payable (Rs)'
    net = view.get('net_inr')
    c = ws.cell(row=5, column=2, value=net if net is not None
                                       else view.get('gross_inr', 0))
    c.number_format = INR_FMT

    headers = ['Role', 'Account', 'Investor', 'Scheme',
               'Period from', 'Period to', 'Days',
               'Avg AUM (Rs)', 'Share %', 'Earnings (Rs)']
    _set_header(ws, 7, headers)

    r = 8
    for role_section in view.get('roles', []):
        role_label = role_section.get('role_label') or role_section.get('role')
        # Sort role rows by earnings descending so the heaviest pools
        # for this entity sit at the top of each role block.
        rows = sorted(role_section.get('rows') or [],
                      key=lambda x: -float(x.get('earnings_inr') or 0))
        for row in rows:
            ws.cell(row=r, column=1, value=role_label)
            ws.cell(row=r, column=2, value=row.get('account_code'))
            ws.cell(row=r, column=3, value=row.get('client_name'))
            ws.cell(row=r, column=4, value=row.get('scheme_name'))
            ws.cell(row=r, column=5, value=row.get('period_from'))
            ws.cell(row=r, column=6, value=row.get('period_to'))
            ws.cell(row=r, column=7, value=row.get('period_days'))
            c = ws.cell(row=r, column=8, value=row.get('avg_aum'))
            c.number_format = INR_FMT
            c = ws.cell(row=r, column=9, value=row.get('share_pct'))
            c.number_format = PCT_FMT
            c = ws.cell(row=r, column=10, value=row.get('earnings_inr'))
            c.number_format = INR_FMT
            r += 1
        # Role subtotal — bold, left-blank up to the Earnings cell.
        ws.cell(row=r, column=9, value=f'{role_label} subtotal').font = (
            __import__('openpyxl').styles.Font(bold=True))
        c = ws.cell(row=r, column=10, value=role_section.get('subtotal_inr'))
        c.number_format = INR_FMT
        c.font = __import__('openpyxl').styles.Font(bold=True)
        r += 2   # blank row between roles

    # Grand total row.
    from openpyxl.styles import Font, PatternFill
    fill = PatternFill('solid', fgColor='F4ECD8')
    ws.cell(row=r, column=9, value='Grand total').font = Font(bold=True)
    ws.cell(row=r, column=9).fill = fill
    c = ws.cell(row=r, column=10, value=view.get('gross_inr', 0))
    c.number_format = INR_FMT
    c.font = Font(bold=True)
    c.fill = fill

    _autosize(ws, len(headers))
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def build_workbook(result: Dict[str, Any]) -> bytes:
    """Build the Excel bytes from a fees-compute response.

    `result` is the dict returned by /api/fees/compute (including
    period, by_role, by_earner, by_account, totals, aum_path, etc.).
    Fee rates and entity payments are read live from the DB / store
    so the export always reflects 'right now', not whatever was
    cached client-side.
    """
    from openpyxl import Workbook
    wb = Workbook()
    # Default sheet that openpyxl creates is empty — replace by removing
    # it once we've added our first named sheet.
    default = wb.active
    _write_summary(wb, result)
    if default in wb.worksheets and default.title == 'Sheet':
        wb.remove(default)
    _write_by_entity(wb, result)
    _write_per_account(wb, result)
    _write_fee_rates(wb)
    _write_entity_payments(wb)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
