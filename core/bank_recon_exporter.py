"""Bank reconciliation Excel exporter.

Writes a three-sheet workbook from a BankReconSummary.to_dict()
payload plus an optional BankBalanceSummary.to_dict() for the
custodian-side balance check.

Sheets:
  1. Summary       — KPI row, totals by status, date, per-bank rollup
  2. Pool Detail   — one row per pool with cust opening/closing,
                     WS opening/closing, variance, L1/L2 status,
                     overall status
  3. Issues        — ws_only_accounts, cust_only_accounts, artificial
                     account summaries if present

The output is intentionally narrow — just enough for an operator to
glance at and dig into breaks. For the full-fidelity report (every
transaction match, bank-by-bank ledgers), Phase 2+ can grow this.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

try:
    import openpyxl
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
except ImportError as e:  # pragma: no cover
    raise RuntimeError(
        "openpyxl is required for bank recon export — check pyproject.toml"
    ) from e


# ── Style palette ────────────────────────────────────────────────────── #

NAVY = "1F3864"
GOLD = "C9A84C"
RED = "C00000"
GREEN = "1A7A4A"
AMBER = "B06820"
GREY = "6B7C93"

FILL_NAVY = PatternFill("solid", fgColor=NAVY)
FILL_GREEN = PatternFill("solid", fgColor="EAF7F0")
FILL_RED = PatternFill("solid", fgColor="FDF1F1")
FILL_AMBER = PatternFill("solid", fgColor="FDF5EB")
FILL_GREY = PatternFill("solid", fgColor="F4F6F9")
# Prior-day tie-out rows: grey when clean, red-tinted when gap > tolerance.
FILL_PRIOR = PatternFill("solid", fgColor="EFEFEF")
FILL_PRIOR_GAP = PatternFill("solid", fgColor="F8CBAD")

# Round-to-paise zero check — replaces the legacy 0.05 tolerance.
# Pool-level classification is driven by overall_status (set by the
# engine using the admin-configured rupee threshold).
def _is_zero(x):
    try:
        return round(abs(float(x or 0.0)), 2) == 0.0
    except (TypeError, ValueError):
        return False

BOLD_WHITE = Font(bold=True, color="FFFFFF", size=11)
BOLD = Font(bold=True, size=11)
HDR_CELL = Font(bold=True, size=9, color="FFFFFF")

CENTER = Alignment(horizontal="center", vertical="center")
LEFT = Alignment(horizontal="left", vertical="center", indent=1)
RIGHT = Alignment(horizontal="right", vertical="center")

THIN = Side(style="thin", color="D0D6E4")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

NUM_FMT = '#,##0.00;[Red]-#,##0.00'


# ── Helpers ──────────────────────────────────────────────────────────── #


def _status_fill(status: str) -> PatternFill:
    s = (status or "").lower()
    if "within tolerance" in s:
        return FILL_AMBER
    if "clean" in s:
        return FILL_GREEN
    if "break" in s:
        return FILL_RED
    if "no statement" in s or "not in ws" in s or "missing" in s:
        return FILL_AMBER
    return FILL_GREY


def _writeline(ws, row: int, cells: list, *, bold: bool = False) -> int:
    for i, value in enumerate(cells, start=1):
        c = ws.cell(row=row, column=i, value=value)
        c.border = BORDER
        if bold:
            c.font = BOLD
        if isinstance(value, (int, float)):
            c.number_format = NUM_FMT
            c.alignment = RIGHT
    return row + 1


# ── Public entry point ─────────────────────────────────────────────── #


def export_bank_recon(
    summary_dict: dict[str, Any],
    out_path: str,
    *,
    balance_check: dict[str, Any] | None = None,
    recon_date: str | None = None,
    prior_closings: dict[str, tuple[str, float]] | None = None,
) -> str:
    """Write a bank recon Excel file and return the path.

    `summary_dict`   must be the shape produced by
                     core.bank_vs_ws_recon.BankReconSummary.to_dict().
    `balance_check`  optional dict from the custodian bank balance
                     engine (core.bank_recon_engine.ReconSummary.to_dict()).
                     When present, a "Cust Balance Check" sheet is
                     produced with one row per cust account.
    `recon_date`     YYYY-MM-DD for the header; falls back to
                     summary_dict['date'] or today.
    `prior_closings` optional mapping of ``cust_account → (date, closing)``
                     for the most recent closing strictly before
                     ``recon_date``. When supplied, a "Prior-Day Closing"
                     row is rendered above each pool in Pool Detail,
                     above each account in Cust Balance Check, and above
                     each block in Ledger. Gaps beyond PRIOR_TOL are
                     highlighted red.
    """
    wb = openpyxl.Workbook()

    recon_date = recon_date or summary_dict.get("date") or datetime.utcnow().strftime("%Y-%m-%d")
    prior_closings = prior_closings or {}

    _build_summary_sheet(wb.active, summary_dict, recon_date)
    _build_detail_sheet(wb.create_sheet("Pool Detail"), summary_dict, prior_closings)
    _build_ledger_sheet(wb.create_sheet("Ledger"), summary_dict, balance_check, prior_closings)
    if balance_check:
        _build_cust_balance_check_sheet(
            wb.create_sheet("Cust Balance Check"), balance_check, prior_closings
        )
    _build_unbooked_sheet(wb.create_sheet("Un-Booked Sells"), summary_dict)
    _build_issues_sheet(wb.create_sheet("Issues"), summary_dict)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    wb.save(out_path)
    return out_path


# ── Sheet 1: Summary ─────────────────────────────────────────────── #


def _build_summary_sheet(ws, summary: dict[str, Any], recon_date: str) -> None:
    ws.title = "Summary"

    total = int(summary.get("total_pools") or 0)
    clean = int(summary.get("clean") or 0)
    breaks = int(summary.get("breaks") or 0)
    other = max(0, total - clean - breaks)

    # Title banner
    ws.merge_cells("A1:F1")
    cell = ws["A1"]
    from core.date_format import display_date as _disp_d
    cell.value = f"Keystone — Bank Reconciliation {_disp_d(recon_date)}"
    cell.font = Font(bold=True, color=NAVY, size=14)
    cell.alignment = LEFT

    # KPI row
    ws["A3"] = "Total pools"
    ws["B3"] = total
    ws["A4"] = "Clean"
    ws["B4"] = clean
    ws["A5"] = "Breaks"
    ws["B5"] = breaks
    ws["A6"] = "Other (no statement / not in WS)"
    ws["B6"] = other

    for r in range(3, 7):
        ws.cell(row=r, column=1).font = BOLD
        ws.cell(row=r, column=1).alignment = LEFT
        ws.cell(row=r, column=2).alignment = RIGHT
        ws.cell(row=r, column=2).number_format = "#,##0"

    # Per-bank rollup
    ws["A8"] = "By bank"
    ws["A8"].font = Font(bold=True, color=NAVY, size=11)

    bank_headers = ["Bank", "Pools", "Clean", "Breaks", "Other"]
    for i, h in enumerate(bank_headers, start=1):
        c = ws.cell(row=9, column=i, value=h)
        c.font = HDR_CELL
        c.fill = FILL_NAVY
        c.alignment = CENTER
        c.border = BORDER

    by_bank: dict[str, dict[str, int]] = {}
    for p in summary.get("pool_results") or []:
        bank = str(p.get("bank") or "—")
        status = str(p.get("overall_status") or "")
        b = by_bank.setdefault(bank, {"pools": 0, "clean": 0, "breaks": 0, "other": 0})
        b["pools"] += 1
        if "clean" in status.lower():
            b["clean"] += 1
        elif "break" in status.lower():
            b["breaks"] += 1
        else:
            b["other"] += 1

    row = 10
    for bank in sorted(by_bank):
        b = by_bank[bank]
        row = _writeline(ws, row, [bank, b["pools"], b["clean"], b["breaks"], b["other"]])

    for i, w in enumerate([32, 10, 10, 10, 10], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


# ── Sheet 2: Pool detail ─────────────────────────────────────────── #


DETAIL_HEADERS = [
    "Strategy",
    "Bank",
    "Pool Account",
    "Cust Opening",
    "WS Opening",
    "Cust Closing",
    "WS Closing",
    "Variance",
    "Status",
    "Note",
]


def _build_detail_sheet(ws, summary: dict[str, Any],
                        prior_closings: dict[str, tuple[str, float]] | None = None) -> None:
    prior_closings = prior_closings or {}

    # Title banner
    ws.merge_cells("A1:J1")
    cell = ws["A1"]
    from core.date_format import display_date as _disp_d
    cell.value = f"Pool Detail — {_disp_d(summary.get('date') or '')}"
    cell.font = Font(bold=True, color=NAVY, size=13)
    cell.alignment = LEFT

    # Header row
    for i, h in enumerate(DETAIL_HEADERS, start=1):
        c = ws.cell(row=3, column=i, value=h)
        c.font = HDR_CELL
        c.fill = FILL_NAVY
        c.alignment = CENTER
        c.border = BORDER

    # Sort by status (breaks first), then by bank, then by strategy
    def _sort_key(p: dict) -> tuple:
        status = (str(p.get("overall_status") or "")).lower()
        if "within tolerance" in status: order = 1  # amber band
        elif "break" in status:          order = 0  # red — action needed
        elif "clean" in status:          order = 2  # green
        else:                             order = 3
        return (order, str(p.get("bank") or ""), str(p.get("strategy_name") or ""))

    pool_rows = sorted(summary.get("pool_results") or [], key=_sort_key)

    row = 4
    for p in pool_rows:
        # Prior-day tie-out row (above the main pool row)
        row = _render_prior_row_detail(ws, row, p, prior_closings)

        values = [
            p.get("strategy_name") or "—",
            p.get("bank") or "—",
            p.get("cust_account") or "—",
            float(p.get("cust_opening") or 0) if p.get("has_opening_balance") else None,
            float(p.get("ws_opening_sum") or 0) if p.get("has_opening_balance") else None,
            float(p.get("cust_closing") or 0),
            float(p.get("ws_closing_sum") or 0),
            float(p.get("l1_variance") or 0),
            p.get("overall_status") or "—",
            p.get("note") or "",
        ]
        fill = _status_fill(str(p.get("overall_status") or ""))
        for i, v in enumerate(values, start=1):
            c = ws.cell(row=row, column=i, value=v)
            c.border = BORDER
            c.fill = fill
            if isinstance(v, float):
                c.number_format = NUM_FMT
                c.alignment = RIGHT
                if i == 8:  # Variance — colour from engine status
                    _st = str(p.get("overall_status") or "")
                    if _st == "Balance Break":
                        c.font = Font(bold=True, color=RED)
                    elif _st in ("Within Tolerance", "Transaction Break", "Settlement Timing"):
                        c.font = Font(bold=True, color="8B6914")
            if i == 10 and v:
                c.alignment = Alignment(wrap_text=True, vertical="center")
        row += 1

    for i, w in enumerate([30, 10, 24, 16, 16, 16, 16, 14, 18, 55], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _render_prior_row_detail(ws, row: int, p: dict,
                             prior_closings: dict[str, tuple[str, float]]) -> int:
    """Render a Prior-Day Closing row above a Pool Detail row.
    Wrapped so a single bad entry can't kill the export — we'd rather
    skip one prior row than fail the download."""
    try:
        acct = p.get("cust_account") or ""
        if not acct:
            return row
        pd = prior_closings.get(acct)
        if not pd:
            return row
        pd_date, pd_close_raw = pd
        pd_close = float(pd_close_raw) if pd_close_raw is not None else 0.0
        cust_open = float(p.get("cust_opening") or 0) if p.get("has_opening_balance") else 0.0
        gap = round(cust_open - pd_close, 2)
        label = f"Prior-Day Closing ({pd_date})"
        if not _is_zero(gap):
            label += f" → opening gap {gap:+,.2f}"
        fill = FILL_PRIOR_GAP if not _is_zero(gap) else FILL_PRIOR
        # Col layout matches DETAIL_HEADERS.
        vals = [label, p.get("bank") or "", acct,
                None, None, pd_close, None, None, "PRIOR", None]
        for i, v in enumerate(vals, start=1):
            c = ws.cell(row=row, column=i, value=v)
            c.fill = fill
            c.border = BORDER
            if isinstance(v, float):
                c.number_format = NUM_FMT
                c.alignment = RIGHT
            if i == 1 and not _is_zero(gap):
                c.font = Font(bold=True, color=RED)
        return row + 1
    except Exception:
        return row


# ── Sheet 2b: Un-Booked Sells ────────────────────────────────────── #


UNBOOKED_HEADERS = [
    "Strategy",
    "Bank",
    "Pool Account",
    "Date",
    "Credit Amount",
    "Description",
    "Status",
]


def _build_unbooked_sheet(ws, summary: dict[str, Any]) -> None:
    """Flat list of every custodian credit flagged as an un-booked sell."""
    ws.merge_cells("A1:G1")
    cell = ws["A1"]
    from core.date_format import display_date as _disp_d
    cell.value = f"Likely un-booked sells — {_disp_d(summary.get('date') or '')}"
    cell.font = Font(bold=True, color=NAVY, size=13)
    cell.alignment = LEFT

    ws.merge_cells("A2:G2")
    intro = ws["A2"]
    intro.value = (
        "Custodian account received a credit on this date but no matching "
        "entry appears in the WS Bank Book — the typical signature of a sell "
        "that settled in the pool but was not yet booked in WealthSpectrum."
    )
    intro.font = Font(size=9, italic=True, color=GREY)
    intro.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[2].height = 30

    for i, h in enumerate(UNBOOKED_HEADERS, start=1):
        c = ws.cell(row=3, column=i, value=h)
        c.font = HDR_CELL
        c.fill = FILL_NAVY
        c.alignment = CENTER
        c.border = BORDER

    rows = []
    for p in summary.get("pool_results") or []:
        for tm in p.get("txn_matches") or []:
            if not tm.get("unbooked_sell"):
                continue
            rows.append((p, tm))

    row = 4
    if not rows:
        ws.cell(row=row, column=1, value="No un-booked sells detected on this date.").font = Font(
            italic=True, color=GREY
        )
    else:
        rows.sort(key=lambda x: (
            str(x[0].get("bank") or ""),
            str(x[0].get("strategy_name") or ""),
            str(x[1].get("date") or ""),
        ))
        for p, tm in rows:
            values = [
                p.get("strategy_name") or "—",
                p.get("bank") or "—",
                p.get("cust_account") or "—",
                tm.get("date") or "—",
                float(tm.get("cust_amount") or 0),
                tm.get("cust_desc") or "",
                p.get("overall_status") or "—",
            ]
            for i, v in enumerate(values, start=1):
                c = ws.cell(row=row, column=i, value=v)
                c.border = BORDER
                c.fill = FILL_AMBER
                if isinstance(v, float):
                    c.number_format = NUM_FMT
                    c.alignment = RIGHT
                    c.font = Font(bold=True)
            row += 1

    for i, w in enumerate([30, 10, 24, 14, 16, 50, 18], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


# ── Sheet 3: Issues ──────────────────────────────────────────────── #


def _build_issues_sheet(ws, summary: dict[str, Any]) -> None:
    ws.merge_cells("A1:D1")
    cell = ws["A1"]
    cell.value = "Issues — accounts not matched between custody and WS"
    cell.font = Font(bold=True, color=NAVY, size=13)
    cell.alignment = LEFT

    row = 3

    ws_only = summary.get("ws_only_accounts") or []
    cust_only = summary.get("cust_only_accounts") or []
    artificial = summary.get("artificial_summaries") or []

    if ws_only:
        ws.cell(row=row, column=1, value="WS accounts with no custodian counterpart").font = Font(
            bold=True, color=AMBER
        )
        row += 1
        headers = ["Strategy / account", "Bank", "Closing balance", "Notes"]
        for i, h in enumerate(headers, start=1):
            c = ws.cell(row=row, column=i, value=h)
            c.font = HDR_CELL
            c.fill = FILL_NAVY
            c.border = BORDER
            c.alignment = CENTER
        row += 1
        for item in ws_only:
            row = _writeline(
                ws,
                row,
                [
                    str(item.get("strategy_name") or item.get("account") or "—"),
                    str(item.get("bank") or "—"),
                    float(item.get("closing_balance") or 0),
                    str(item.get("note") or ""),
                ],
            )
        row += 2

    if cust_only:
        ws.cell(row=row, column=1, value="Custodian accounts with no WS counterpart").font = Font(
            bold=True, color=AMBER
        )
        row += 1
        headers = ["Account", "Bank", "Closing balance", "Notes"]
        for i, h in enumerate(headers, start=1):
            c = ws.cell(row=row, column=i, value=h)
            c.font = HDR_CELL
            c.fill = FILL_NAVY
            c.border = BORDER
            c.alignment = CENTER
        row += 1
        for item in cust_only:
            row = _writeline(
                ws,
                row,
                [
                    str(item.get("account_no") or item.get("account") or "—"),
                    str(item.get("bank") or "—"),
                    float(item.get("closing_balance") or item.get("balance") or 0),
                    str(item.get("note") or item.get("status") or ""),
                ],
            )
        row += 2

    if artificial:
        ws.cell(row=row, column=1, value="Artificial / float accounts").font = Font(
            bold=True, color=NAVY
        )
        row += 1
        headers = ["Bank", "Account", "Balance", "Unallocated"]
        for i, h in enumerate(headers, start=1):
            c = ws.cell(row=row, column=i, value=h)
            c.font = HDR_CELL
            c.fill = FILL_NAVY
            c.border = BORDER
            c.alignment = CENTER
        row += 1
        for item in artificial:
            row = _writeline(
                ws,
                row,
                [
                    str(item.get("bank") or "—"),
                    str(item.get("account_no") or "—"),
                    float(item.get("balance") or 0),
                    float(item.get("unallocated") or 0),
                ],
            )
        row += 2

    if row == 3:
        ws.cell(row=row, column=1, value="No unmatched accounts on this date.").font = Font(
            italic=True, color=GREY
        )

    for i, w in enumerate([40, 14, 18, 30], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


# ── Sheet 4: Ledger ──────────────────────────────────────────────── #


def _build_ledger_sheet(ws, summary: dict[str, Any],
                        balance_check: dict[str, Any] | None,
                        prior_closings: dict[str, tuple[str, float]]) -> None:
    """Standardised 4-row block per pool: Opening / Debits / Credits / Closing.
    When prior-day data is available, a 'Prior-Day Closing' row is inserted
    above the Opening row to surface the tie-out gap.
    """
    ws.merge_cells("A1:E1")
    cell = ws["A1"]
    from core.date_format import display_date as _disp_d
    cell.value = f"Bank Reconciliation Ledger — {_disp_d(summary.get('date') or '')}"
    cell.font = Font(bold=True, color=NAVY, size=13)
    cell.alignment = LEFT

    # Background palette per row-type.
    OFIL  = PatternFill("solid", fgColor="EBF0F8")  # opening — pale blue
    DFIL  = PatternFill("solid", fgColor="FFF3F3")  # debits — pale red
    CRFIL = PatternFill("solid", fgColor="F0FFF4")  # credits — pale green
    CFIL2 = PatternFill("solid", fgColor="E8F5E9")  # closing — green

    HDR_ROW_LABELS = ["", "BANK AMOUNT", "WS AMOUNT", "DIFFERENCE", "STATUS"]

    # Build lookup: account_no → custodian balance-check row
    acct_lookup: dict[str, dict[str, Any]] = {}
    if balance_check:
        for ar in balance_check.get("results") or []:
            acct_lookup[str(ar.get("account_no") or "")] = ar

    def _sort_key(p: dict) -> tuple:
        status = (str(p.get("overall_status") or "")).lower()
        if "within tolerance" in status: order = 1  # amber band
        elif "break" in status:          order = 0  # red — action needed
        elif "clean" in status:          order = 2  # green
        else:                             order = 3
        return (order, str(p.get("bank") or ""), str(p.get("strategy_name") or ""))

    status_display = {
        "Clean": "CLEAR",
        "Within Tolerance": "WITHIN TOL",
        "Balance Break": "BREAK",
        "Transaction Break": "TXN BREAK",
        "No Statement": "NO STMT",
        "Not in WS": "NO WS DATA",
    }

    row = 2
    for p in sorted(summary.get("pool_results") or [], key=_sort_key):
        strategy = str(p.get("strategy_name") or "—")
        bank = str(p.get("bank") or "—")
        pool_acct = str(p.get("cust_account") or "—")
        status = str(p.get("overall_status") or "")

        # Pool header (merged banner)
        row += 1
        ws.merge_cells(f"A{row}:E{row}")
        hc = ws.cell(row=row, column=1, value=f"{strategy}  |  {bank}  |  Pool: {pool_acct}")
        hc.font = Font(bold=True, size=11, color="FFFFFF")
        hc.fill = FILL_NAVY
        hc.alignment = Alignment(vertical="center", indent=1)
        ws.row_dimensions[row].height = 18

        # Prior-Day Closing row (if history has a record for this account)
        try:
            pd = prior_closings.get(pool_acct)
            if pd:
                pd_date, pd_close_raw = pd
                pd_close = float(pd_close_raw) if pd_close_raw is not None else 0.0
                cust_open = float(p.get("cust_opening") or 0) if p.get("has_opening_balance") else 0.0
                gap = round(cust_open - pd_close, 2)
                row += 1
                fill = FILL_PRIOR_GAP if not _is_zero(gap) else FILL_PRIOR
                label = f"Prior-Day Closing ({pd_date})"
                if not _is_zero(gap):
                    label += f" → gap {gap:+,.2f}"
                gap_val = gap if not _is_zero(gap) else None
                for col, v in enumerate([label, pd_close, None, gap_val, ""], start=1):
                    c = ws.cell(row=row, column=col, value=v)
                    c.fill = fill
                    c.border = BORDER
                    if col == 1:
                        c.font = Font(bold=True,
                                      color=(RED if not _is_zero(gap) else NAVY))
                    if isinstance(v, float):
                        c.number_format = NUM_FMT
                        c.alignment = RIGHT
                        if col == 4 and not _is_zero(v):
                            c.font = Font(bold=True, color=RED)
        except Exception:
            pass

        # Header row
        row += 1
        for col, h in enumerate(HDR_ROW_LABELS, start=1):
            c = ws.cell(row=row, column=col, value=h)
            c.font = BOLD_WHITE
            c.fill = FILL_NAVY
            c.border = BORDER

        # Pull custodian numbers from balance_check when available,
        # otherwise derive from pool-side txn_matches.
        ar = acct_lookup.get(pool_acct)
        has_open = p.get("has_opening_balance")
        if has_open and ar:
            op_b = float(ar.get("opening_balance") or 0)
            dr_b = float(ar.get("total_debits") or 0)
            cr_b = float(ar.get("total_credits") or 0)
        elif has_open:
            op_b = float(p.get("cust_opening") or 0)
            dr_b = round(sum(-t.get("cust_amount", 0) for t in (p.get("txn_matches") or [])
                             if (t.get("cust_amount") or 0) < 0), 2)
            cr_b = round(sum(t.get("cust_amount", 0) for t in (p.get("txn_matches") or [])
                             if (t.get("cust_amount") or 0) > 0), 2)
        else:
            op_b = 0.0; dr_b = 0.0; cr_b = 0.0
        cl_b = float(p.get("cust_closing") or 0)

        # WS side — no per-txn breakdown available, split the net.
        if has_open:
            op_ws = float(p.get("ws_opening_sum") or 0)
            ws_net = round(float(p.get("ws_closing_sum") or 0) - op_ws, 2)
            dr_ws = round(max(0.0, -ws_net), 2)
            cr_ws = round(max(0.0, ws_net), 2)
        else:
            op_ws = 0.0; dr_ws = 0.0; cr_ws = 0.0
        cl_ws = float(p.get("ws_closing_sum") or 0)

        def _row(lr: int, label: str, bv: float, wsv: float, fill: PatternFill) -> int:
            lr += 1
            diff = round((bv or 0.0) - (wsv or 0.0), 2)
            for col, v in enumerate([label, bv, wsv, diff, ""], start=1):
                c = ws.cell(row=lr, column=col, value=v)
                c.fill = fill
                c.border = BORDER
                if col == 1:
                    c.font = BOLD
                elif isinstance(v, float):
                    c.number_format = NUM_FMT
                    c.alignment = RIGHT
                    if col == 4 and not _is_zero(v):
                        c.font = Font(bold=True, color=RED)
            return lr

        row = _row(row, "Opening Balance", op_b, op_ws, OFIL)
        row = _row(row, "Total Debits",    dr_b, dr_ws, DFIL)
        row = _row(row, "Total Credits",   cr_b, cr_ws, CRFIL)

        # Closing row with status tag
        row += 1
        cl_diff = round((cl_b or 0.0) - (cl_ws or 0.0), 2)
        status_col = status_display.get(status, status)
        for col, v in enumerate(["Closing Balance", cl_b, cl_ws, cl_diff, status_col], start=1):
            c = ws.cell(row=row, column=col, value=v)
            c.fill = CFIL2
            c.border = BORDER
            c.font = BOLD
            if isinstance(v, float):
                c.number_format = NUM_FMT
                c.alignment = RIGHT
                if col == 4 and not _is_zero(v):
                    c.font = Font(bold=True, color=RED)
        row += 1  # blank gap after each pool block

    for i, w in enumerate([26, 16, 16, 14, 14], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


# ── Sheet 5: Cust Balance Check ──────────────────────────────────── #


CUST_BAL_HEADERS = [
    "Bank", "Account No", "Account Name", "As On",
    "Opening", "Credits", "Debits", "Net",
    "Computed", "Closing", "Variance", "Status",
]


def _build_cust_balance_check_sheet(ws, balance_check: dict[str, Any],
                                    prior_closings: dict[str, tuple[str, float]]) -> None:
    """Custodian-side balance check — one row per account, with a
    'Prior-Day Closing' row above each main row showing the tie-out
    gap against the last-recorded closing in the history log.
    """
    ws.merge_cells("A1:L1")
    cell = ws["A1"]
    from core.date_format import display_date as _disp_d
    cell.value = f"Custodian Balance Check — {_disp_d(balance_check.get('date') or '')}"
    cell.font = Font(bold=True, color=NAVY, size=13)
    cell.alignment = LEFT

    for i, h in enumerate(CUST_BAL_HEADERS, start=1):
        c = ws.cell(row=3, column=i, value=h)
        c.font = HDR_CELL
        c.fill = FILL_NAVY
        c.alignment = CENTER
        c.border = BORDER

    def _safe_float(x) -> float:
        try:
            return float(x) if x is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _status_fill_balance(status: str) -> PatternFill:
        s = (status or "").upper()
        if s in ("MATCH", "CLEAR"):
            return FILL_GREEN
        if s in ("MISMATCH", "BREAK"):
            return FILL_RED
        return FILL_GREY

    results = sorted(
        balance_check.get("results") or [],
        key=lambda r: (str(r.get("bank") or ""), str(r.get("account_no") or "")),
    )

    row = 3
    for res in results:
        acct_no = str(res.get("account_no") or "")
        # Prior-day row (wrapped so one bad entry can't fail the export)
        try:
            pd = prior_closings.get(acct_no)
            if pd:
                pd_date, pd_close_raw = pd
                pd_close = _safe_float(pd_close_raw)
                cust_open = _safe_float(res.get("opening_balance"))
                gap = round(cust_open - pd_close, 2)
                row += 1
                fill = FILL_PRIOR_GAP if not _is_zero(gap) else FILL_PRIOR
                label = f"Prior-Day Closing ({pd_date})"
                if not _is_zero(gap):
                    label += f" → today opening gap: {gap:+,.2f}"
                vals = [res.get("bank") or "", acct_no, label, pd_date,
                        None, None, None, None, None, pd_close, None, "PRIOR"]
                for col, v in enumerate(vals, start=1):
                    c = ws.cell(row=row, column=col, value=v)
                    c.fill = fill
                    c.border = BORDER
                    if isinstance(v, float):
                        c.number_format = NUM_FMT
                        c.alignment = RIGHT
                    if col == 3 and not _is_zero(gap):
                        c.font = Font(bold=True, color=RED)
        except Exception:
            pass

        # Main row
        row += 1
        status = str(res.get("status") or "")
        fill = _status_fill_balance(status)
        net = _safe_float(res.get("net_movement",
                                  _safe_float(res.get("total_credits"))
                                  - _safe_float(res.get("total_debits"))))
        vals = [
            res.get("bank") or "",
            acct_no,
            res.get("account_name") or "",
            res.get("as_on_date") or "",
            _safe_float(res.get("opening_balance")),
            _safe_float(res.get("total_credits")),
            _safe_float(res.get("total_debits")),
            net,
            _safe_float(res.get("computed_closing")),
            _safe_float(res.get("closing_balance")),
            _safe_float(res.get("variance")),
            status,
        ]
        for col, v in enumerate(vals, start=1):
            c = ws.cell(row=row, column=col, value=v)
            c.fill = fill
            c.border = BORDER
            if isinstance(v, float):
                c.number_format = NUM_FMT
                c.alignment = RIGHT
                if col == 11 and not _is_zero(v):  # Variance
                    c.font = Font(bold=True, color=RED)

    for i, w in enumerate([10, 18, 36, 13, 16, 16, 16, 14, 16, 16, 14, 12], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
