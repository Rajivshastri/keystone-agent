# ws_uploader.py
# ─────────────────────────────────────────────────────────────────────────────
# WealthSpectrum file uploader — handles multi-step form submissions.
#
# Reuses the AES-128-CBC login from ws_downloader.py but with optional
# separate credentials for upload operations.
#
# Environment variables:
#   FINCRM_UPLOAD_USER / FINCRM_UPLOAD_PASS — upload-specific credentials
#   Falls back to FINCRM_USER / FINCRM_PASS if not set.
#   FINCRM_URL = https://app.thegoldstandard.in/fincrm  (default)
# ─────────────────────────────────────────────────────────────────────────────

import os
import re
import logging
from pathlib import Path
from dataclasses import dataclass

import requests as _requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


def _display_date(date_str: str) -> str:
    """Convert YYYY-MM-DD to MM-DD-YYYY for display in emails and reports."""
    try:
        parts = date_str.split("-")
        if len(parts) == 3:
            return f"{parts[1]}-{parts[2]}-{parts[0]}"
    except Exception:
        pass
    return date_str


# ── Credentials ──────────────────────────────────────────────────────────────

def _base_url() -> str:
    return os.environ.get("FINCRM_URL", "https://app.thegoldstandard.in/fincrm").rstrip("/")

def _upload_user() -> str:
    return os.environ.get("FINCRM_UPLOAD_USER") or os.environ.get("FINCRM_USER", "")

def _upload_pass() -> str:
    return os.environ.get("FINCRM_UPLOAD_PASS") or os.environ.get("FINCRM_PASS", "")

def upload_creds_configured() -> bool:
    return bool(_upload_user() and _upload_pass())


# ── Login (reuse ws_downloader's AES login, but with upload creds) ───────────

def _login_upload(session: _requests.Session, auth_cache: Path) -> None:
    """Log in with upload credentials. Temporarily swaps env vars if upload-
    specific creds are set, then delegates to ws_downloader._login()."""
    from ws_downloader import _login

    orig_user = os.environ.get("FINCRM_USER", "")
    orig_pass = os.environ.get("FINCRM_PASS", "")
    try:
        os.environ["FINCRM_USER"] = _upload_user()
        os.environ["FINCRM_PASS"] = _upload_pass()
        _login(session, auth_cache)
    finally:
        # Restore originals so download flows aren't affected
        if orig_user:
            os.environ["FINCRM_USER"] = orig_user
        if orig_pass:
            os.environ["FINCRM_PASS"] = orig_pass


# ── Result type ──────────────────────────────────────────────────────────────

@dataclass
class UploadResult:
    ok: bool
    message: str
    detail: str = ""

    def to_dict(self):
        return {"ok": self.ok, "message": self.message, "detail": self.detail}


# ── HTML helpers ─────────────────────────────────────────────────────────────

def _scrape_all_inputs(html: str) -> dict:
    """Extract every <input> name=value from the page."""
    soup = BeautifulSoup(html, "html.parser")
    data = {}
    for inp in soup.find_all("input"):
        name = inp.get("name")
        if name and inp.get("type") not in ("submit", "reset", "button", "file"):
            data[name] = inp.get("value", "") or ""
    return data


def _scrape_csrf(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    inp = soup.find("input", {"name": "WS_CSRFTOKEN"})
    return inp.get("value", "") if inp else ""


# ── Result parser ────────────────────────────────────────────────────────────

def _parse_posting_result(html: str) -> dict:
    """Parse the Trade Posting result page to extract record counts.

    The result table has rows like:
      <TD><b>Total Records</b></TD>
      <TD>42</TD>
    """
    soup = BeautifulSoup(html, "html.parser")
    counts = {}

    label_map = {
        "total records":            "total",
        "processed records":        "processed",
        "validation error records": "validation_errors",
        "parsing error records":    "parsing_errors",
        "error details":            "error_details",
        "postingid":                "posting_id",
    }

    for td in soup.find_all("td"):
        b = td.find("b")
        if not b:
            continue
        label = b.get_text(strip=True).lower()
        key = label_map.get(label)
        if not key:
            continue
        # Value is in the next sibling <td>
        val_td = td.find_next_sibling("td")
        if not val_td:
            continue
        raw = val_td.get_text(strip=True)
        if key in ("total", "processed", "validation_errors", "parsing_errors"):
            try:
                counts[key] = int(re.sub(r"[^\d]", "", raw) or "0")
            except ValueError:
                counts[key] = 0
        elif key == "posting_id":
            counts[key] = raw.split()[0] if raw else ""
        else:
            counts[key] = raw[:500]

    return counts


# ── 0096 Upload ──────────────────────────────────────────────────────────────

UPLOAD_FORM_URL = "redirect.do?target=queryTradePosting&scope=*&cmScope=*&menuDisp=N"
MAP_ID_0096 = "96"


def upload_0096(file_path: str, progress_cb=None, config_dir: Path = None) -> UploadResult:
    """
    Upload a 0096 XLS file to WealthSpectrum.

    Three-step flow:
      1. GET  upload form           → scrape CSRF + form action
      2. POST file (multipart)      → server stores temp file, returns Trade Posting page
      3. POST with mapid=96         → duplicate check + execute

    progress_cb(stage, detail): optional callback for UI progress updates.
    """
    fpath = Path(file_path)
    if not fpath.exists():
        return UploadResult(False, f"File not found: {fpath}")

    base = _base_url()
    if config_dir is None:
        config_dir = Path(__file__).parent / "config"
    auth_cache = config_dir / "ws_upload_auth.json"

    session = _requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"{base}/",
    })

    # ── Login ────────────────────────────────────────────────────────────
    if progress_cb:
        progress_cb("login", "Logging in to WealthSpectrum...")
    try:
        _login_upload(session, auth_cache)
    except Exception as e:
        return UploadResult(False, f"Login failed: {e}")

    # ── Step 1: GET the upload form ──────────────────────────────────────
    if progress_cb:
        progress_cb("form", "Loading upload form...")
    form_url = f"{base}/{UPLOAD_FORM_URL}"
    try:
        r1 = session.get(form_url, timeout=30)
        r1.raise_for_status()
    except Exception as e:
        return UploadResult(False, f"Failed to load upload form: {e}")

    soup1 = BeautifulSoup(r1.text, "html.parser")
    form1 = soup1.find("form")
    if not form1:
        return UploadResult(False, "No form found on upload page")

    action1 = form1.get("action", "")
    hidden1 = {}
    for inp in soup1.find_all("input"):
        name = inp.get("name")
        if name and inp.get("type") in ("hidden", None):
            hidden1[name] = inp.get("value", "") or ""
    hidden1["sname"] = fpath.name
    log.info(f"Step 1 OK — action={action1}, fields={list(hidden1.keys())}")

    # ── Step 2: POST the file ────────────────────────────────────────────
    if progress_cb:
        progress_cb("upload", f"Uploading {fpath.name}...")
    post_url = f"{base}/{action1}" if not action1.startswith("http") else action1
    try:
        with open(fpath, "rb") as f:
            files = {"filepath": (fpath.name, f, "application/vnd.ms-excel")}
            r2 = session.post(post_url, data=hidden1, files=files, timeout=120,
                              headers={"Referer": form_url})
        r2.raise_for_status()
    except Exception as e:
        return UploadResult(False, f"File upload failed: {e}")

    # Parse step 2 response — should be the "Trade Posting" form
    fields2 = _scrape_all_inputs(r2.text)
    if "tempfile" not in fields2:
        # Check for error messages
        soup2 = BeautifulSoup(r2.text, "html.parser")
        err = soup2.find(string=re.compile(r"error|invalid|fail", re.I))
        detail = err.strip()[:200] if err else r2.text[:300]
        return UploadResult(False, "Upload form did not return expected Trade Posting page",
                            detail)

    log.info(f"Step 2 OK — tempfile={fields2.get('tempfile')}")

    # ── Step 3a: Duplicate check ─────────────────────────────────────────
    if progress_cb:
        progress_cb("checking", "Checking for duplicates...")

    fields2["mapid"] = MAP_ID_0096
    fields2["mode"] = "checkDuplicateFile"
    fields2["actionString"] = ""

    exec_url = f"{base}/queryTradePosting.do"
    try:
        r3a = session.post(exec_url, data=fields2, timeout=60,
                           headers={"Referer": post_url})
        r3a.raise_for_status()
    except Exception as e:
        return UploadResult(False, f"Duplicate check failed: {e}")

    # The AJAX response updates the actionString hidden field.
    # Parse the response to find the actionString value.
    dup_fields = _scrape_all_inputs(r3a.text)
    action_str = dup_fields.get("actionString", "")

    # Also check the raw text in case it's a partial HTML snippet
    if not action_str:
        m = re.search(r'name=["\']actionString["\'].*?value=["\']([^"\']*)', r3a.text)
        if m:
            action_str = m.group(1)

    log.info(f"Step 3a — actionString={action_str!r}")

    if action_str == "nextWithCancel":
        return UploadResult(False,
                            "WS rejected: file already uploaded and duplicates are not allowed for this map")

    # "next" = no duplicate, "nextWithOKContinue" = duplicate but allowed to proceed
    if action_str not in ("next", "nextWithOKContinue", ""):
        log.warning(f"Unexpected actionString: {action_str!r} — proceeding anyway")

    # ── Step 3b: Execute posting ─────────────────────────────────────────
    if progress_cb:
        progress_cb("posting", "Posting transactions...")

    # Refresh fields from the duplicate check response (may have new CSRF)
    exec_fields = _scrape_all_inputs(r3a.text) if dup_fields else dict(fields2)
    exec_fields["mapid"] = MAP_ID_0096
    exec_fields["mode"] = "errorPage"
    # The JS submits to a wait page, but the actual POST goes to the same .do
    try:
        r3b = session.post(exec_url, data=exec_fields, timeout=180,
                           headers={"Referer": post_url})
        r3b.raise_for_status()
    except Exception as e:
        return UploadResult(False, f"Posting execution failed: {e}")

    # Parse result — WS returns a table with labelled rows
    counts = _parse_posting_result(r3b.text)
    log.info(f"Step 3b — counts: {counts}")

    # Save for debugging
    dump_dir = config_dir.parent / "ws_upload_probe" if config_dir else Path(__file__).parent / "data" / "ws_upload_probe"
    dump_dir.mkdir(parents=True, exist_ok=True)
    (dump_dir / "step3_result.html").write_text(r3b.text, encoding="utf-8", errors="replace")

    total     = counts.get("total", 0)
    processed = counts.get("processed", 0)
    val_err   = counts.get("validation_errors", 0)
    parse_err = counts.get("parsing_errors", 0)
    error_det = counts.get("error_details", "")
    posting_id = counts.get("posting_id", "")
    was_dup   = action_str == "nextWithOKContinue"

    if progress_cb:
        progress_cb("done", "Upload complete")

    if was_dup and total == 0 and processed == 0:
        # Known WS quirk: re-uploading an already-posted file yields 0 records
        # and often a NullPointerException parsing error. Not a real failure.
        msg = "Re-upload: 0 new records (file was already posted)"
        if posting_id:
            msg += f" — Posting ID: {posting_id}"
        return UploadResult(True, msg, "duplicate")

    if parse_err > 0 or val_err > 0:
        msg = (f"Uploaded with errors: {processed} processed, "
               f"{val_err} validation errors, {parse_err} parsing errors")
        return UploadResult(False, msg, error_det)

    msg = f"{processed} of {total} records posted"
    if posting_id:
        msg += f" — Posting ID: {posting_id}"
    return UploadResult(True, msg, "")


# ── Post-Trade-Recon Dispatch ─────────────────────────────────────────────────

@dataclass
class DispatchResult:
    ok: bool
    upload_result: UploadResult
    custodian_results: list  # [{custodian, file, email_ok, error}]

    def to_dict(self):
        return {
            "ok": self.ok,
            "upload": self.upload_result.to_dict(),
            "custodians": self.custodian_results,
        }


def _load_dispatch_config(config_dir: Path = None) -> dict:
    """Load dispatch config from the given config directory."""
    import json
    if config_dir is None:
        config_dir = Path(__file__).parent / "config"
    config_path = config_dir / "custodian_dispatch.json"
    if config_path.exists():
        return json.loads(config_path.read_text())
    return {"custodians": {}}


def _involved_mapins_by_custodian(date_str: str,
                                   config_dir: Path = None,
                                   workdir: Path = None,
                                   file_0096: str = None) -> dict:
    """Determine which MAPINs actually traded today, grouped by custodian.

    Reads the 0096 XLS file directly to extract the MAPIN column
    (column 18, 0-indexed = the mapin_id field). Only MAPINs that
    appear in the 0096 output are returned — if only HDFC pools
    traded, only HDFC shows up. No fallback to "all pools".

    Returns: {custodian: [{'mapin': ..., 'strategy': ...}, ...]}
    """
    import json
    from collections import defaultdict

    if config_dir is None:
        config_dir = Path(__file__).parent / "config"
    if workdir is None:
        workdir = Path(__file__).parent

    hub_path = config_dir / "pools_hub.json"
    if not hub_path.exists():
        return {}
    hub = json.loads(hub_path.read_text())
    mapin_to_cust: dict[str, str] = {}
    mapin_to_strategy: dict[str, str] = {}
    for pool in hub.get("pools", []):
        mapin = (pool.get("mapin") or "").strip()
        cust  = (pool.get("custodian_bank") or "").strip().upper()
        name  = (pool.get("display_name") or pool.get("pool_id") or mapin)
        if mapin and cust:
            mapin_to_cust[mapin] = cust
            mapin_to_strategy[mapin] = name

    # Find the 0096 file — either passed explicitly or search output dir
    fpath = None
    if file_0096:
        fpath = Path(file_0096)
        if not fpath.exists():
            fpath = None
    if fpath is None:
        out_dir = workdir / "data" / date_str / "output"
        candidates = sorted(out_dir.glob("*0096*.xls"), reverse=True) if out_dir.exists() else []
        if candidates:
            fpath = candidates[0]

    if fpath is None or not fpath.exists():
        log.warning("No 0096 file found — cannot determine involved MAPINs")
        return {}

    # Read MAPIN column from the 0096 XLS
    mapins_in_file: set[str] = set()
    try:
        ext = str(fpath).lower().rsplit(".", 1)[-1]
        if ext == "xls":
            import xlrd
            wb = xlrd.open_workbook(str(fpath))
            ws = wb.sheet_by_index(0)
            # MAPIN is column index 17 (0-based) in the 0096 format
            for rx in range(1, ws.nrows):
                val = str(ws.cell_value(rx, 17) if ws.ncols > 17 else "").strip()
                if val:
                    mapins_in_file.add(val)
            wb.release_resources()
        else:
            import openpyxl
            wb = openpyxl.load_workbook(str(fpath), read_only=True, data_only=True)
            ws_sheet = wb[wb.sheetnames[0]]
            for row in ws_sheet.iter_rows(min_row=2, values_only=True):
                if row and len(row) > 17:
                    val = str(row[17] or "").strip()
                    if val:
                        mapins_in_file.add(val)
            wb.close()
    except Exception as e:
        log.warning(f"Failed to read MAPINs from 0096 file: {e}")
        return {}

    if not mapins_in_file:
        log.info("0096 file has no MAPIN rows — no dispatch needed")
        return {}

    result: dict = defaultdict(list)
    for m in mapins_in_file:
        c = mapin_to_cust.get(m)
        if c:
            result[c].append({"mapin": m, "strategy": mapin_to_strategy.get(m, m)})

    return dict(result)


def dispatch_trades(file_0096: str, date_str: str,
                    progress_cb=None,
                    config_dir: Path = None,
                    workdir: Path = None,
                    azure_config: dict = None,
                    by_custodian: dict = None) -> DispatchResult:
    """
    Full post-trade-recon automation:
      1. Upload 0096 file to WS
      2. Download Custody Interface file per involved custodian
      3. Email each file to the custodian

    config_dir:    resolved config directory (agent.paths.config_dir())
    workdir:       resolved workdir (agent settings.workdir)
    azure_config:  M365 Graph credentials dict with tenant_id, client_id,
                   client_secret, mailbox — passed through to the email
                   sender so it doesn't need to read azure.json.
    progress_cb(stage, detail): optional callback.
    """
    from datetime import datetime

    if config_dir is None:
        config_dir = Path(__file__).parent / "config"
    if workdir is None:
        workdir = Path(__file__).parent

    # Step 1: Upload 0096
    upload_result = upload_0096(file_0096, progress_cb=progress_cb, config_dir=config_dir)
    if not upload_result.ok and upload_result.detail != "duplicate":
        return DispatchResult(False, upload_result, [])

    # Determine date in DD/MM/YYYY for WS forms
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        ws_date = dt.strftime("%d/%m/%Y")
    except ValueError:
        ws_date = date_str

    # Step 2: Determine involved MAPINs — caller can pass pre-resolved
    # mapping, otherwise fall back to reading the 0096 file.
    if by_custodian is None:
        by_custodian = _involved_mapins_by_custodian(
            date_str, config_dir=config_dir, workdir=workdir, file_0096=file_0096,
        )
    config = _load_dispatch_config(config_dir=config_dir)
    custodian_cfg = config.get("custodians", {})
    common_cfg    = config.get("common", {})

    total_mapins = sum(len(v) for v in by_custodian.values())
    if progress_cb:
        progress_cb("dispatch",
                     f"Dispatching {total_mapins} MAPIN(s) across "
                     f"{len(by_custodian)} custodian(s): "
                     f"{', '.join(sorted(by_custodian.keys()))}")

    # Step 3: Login to WS for custody interface downloads
    from ws_downloader import _login as _dl_login, _base_url as _dl_base
    auth_cache = config_dir / "ws_auth.json"
    dl_session = _requests.Session()
    dl_session.headers.update({
        "User-Agent": "Mozilla/5.0 WS-Dispatch/1.0",
        "Referer": f"{_dl_base()}/",
    })

    try:
        _dl_login(dl_session, auth_cache)
    except Exception as e:
        return DispatchResult(False, upload_result,
                              [{"custodian": c, "error": f"Download login failed: {e}"}
                               for c in by_custodian])

    base    = _dl_base()
    out_dir = workdir / "data" / date_str / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    custodian_results = []

    # Step 4: Download per MAPIN, email per custodian
    for cust in sorted(by_custodian):
        cfg        = custodian_cfg.get(cust, {})
        iface_type = cfg.get("interface_type")
        if not iface_type:
            custodian_results.append({
                "custodian": cust, "error": f"No interface_type configured for {cust}",
                "files": [], "email_ok": False,
            })
            continue

        fmt = cfg.get("report_format", "X")
        ext = "xlsx" if fmt == "XX" else "csv" if fmt == "C" else "xls"
        mapin_entries = by_custodian[cust]
        downloaded_files = []   # (mapin, strategy, file_path)

        for entry in mapin_entries:
            mapin    = entry['mapin']
            strategy = entry['strategy']
            safe_m   = mapin.replace(" ", "_")
            fname    = f"CustodyInterface_{cust}_{safe_m}_{date_str}.{ext}"
            fpath    = out_dir / fname

            if progress_cb:
                progress_cb("download", f"Downloading {cust} / {mapin}...")

            try:
                from ws_downloader import download_custody_interface
                download_custody_interface(dl_session, base, iface_type, fmt,
                                           common_cfg, ws_date, fpath,
                                           mapinid=mapin)
                downloaded_files.append((mapin, strategy, fpath))
            except Exception as e:
                log.warning(f"Download failed for {cust}/{mapin}: {e}")
                custodian_results.append({
                    "custodian": cust, "mapin": mapin,
                    "error": f"Download failed: {e}",
                    "files": [], "email_ok": False,
                })

        if not downloaded_files:
            continue

        # Send ONE email per custodian with all MAPIN files as attachments
        email_to    = cfg.get("email_to", [])
        send_from   = cfg.get("send_from", "")
        subject_tpl = cfg.get("email_subject", "Trade Allocation - {mapin} - {date}")
        body_tpl    = cfg.get("email_body",
                              "Dear team,\n\nPlease find attached the trade allocation "
                              "instructions for {mapin} for trades dated {date}.\n\n"
                              "Regards,\nGoldStandard Wealth Pvt Ltd")

        # Build subject: list all MAPINs. Display date as MM-DD-YYYY.
        mapin_list = ', '.join(m for m, _, _ in downloaded_files)
        display_date = _display_date(date_str)
        subject = subject_tpl.replace("{mapin}", mapin_list).replace("{date}", display_date).replace("{custodian}", cust)
        body    = body_tpl.replace("{mapin}", mapin_list).replace("{date}", display_date).replace("{custodian}", cust)

        file_names = [fp.name for _, _, fp in downloaded_files]

        if not email_to:
            custodian_results.append({
                "custodian": cust, "files": file_names, "email_ok": False,
                "error": f"No email_to configured — {len(downloaded_files)} file(s) downloaded but not emailed",
            })
            continue

        if progress_cb:
            progress_cb("email", f"Emailing {cust} ({', '.join(email_to)})...")

        try:
            attachment_paths = [fp for _, _, fp in downloaded_files]
            _send_custodian_email(email_to, subject, attachment_paths, cust,
                                  date_str, body_html=body, send_from=send_from,
                                  azure_config=azure_config)
            custodian_results.append({
                "custodian": cust, "files": file_names, "email_ok": True, "error": None,
            })
        except Exception as e:
            custodian_results.append({
                "custodian": cust, "files": file_names, "email_ok": False,
                "error": f"Email failed: {e}",
            })

    all_ok = upload_result.ok and all(
        r.get("email_ok") or not r.get("error") for r in custodian_results
    )
    if progress_cb:
        progress_cb("complete", "Dispatch complete")
    return DispatchResult(all_ok, upload_result, custodian_results)


def _send_custodian_email(recipients: list, subject: str,
                          attachment_paths, custodian: str,
                          date_str: str, body_html: str = '',
                          send_from: str = '',
                          azure_config: dict = None):
    """Send custody interface file(s) to a custodian via M365 Graph API.

    attachment_paths: single Path or list of Paths
    send_from: mailbox to send from (Graph API /users/{send_from}/sendMail).
               Defaults to the configured mailbox.
    azure_config: dict with tenant_id, client_id, client_secret, mailbox.
                  When provided, used directly. When None, falls back to
                  env vars (legacy Flask path).
    """
    import os

    from core.email_ingestor import EmailIngestor

    if azure_config and all(azure_config.values()):
        cfg = azure_config
    else:
        cfg = {
            "tenant_id":     os.environ.get("AZURE_TENANT_ID", ""),
            "client_id":     os.environ.get("AZURE_CLIENT_ID", ""),
            "client_secret": os.environ.get("AZURE_CLIENT_SECRET", ""),
            "mailbox":       os.environ.get("AZURE_MAILBOX", ""),
        }
    ingestor = EmailIngestor(cfg)
    if not ingestor.is_configured():
        raise RuntimeError("Azure email credentials not configured")

    import base64

    # Build attachment list — support single path or list of paths
    if isinstance(attachment_paths, (str, Path)):
        attachment_paths = [Path(attachment_paths)]
    attachments = []
    for ap in attachment_paths:
        ap = Path(ap)
        attachments.append({
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": ap.name,
            "contentType": "application/vnd.ms-excel",
            "contentBytes": base64.b64encode(ap.read_bytes()).decode("ascii"),
        })

    # Format body as HTML (convert newlines to <br>)
    if not body_html:
        body_html = (
            f"Dear team,<br><br>"
            f"Please find attached the trade allocation instructions for "
            f"<strong>{custodian}</strong> for trades dated <strong>{date_str}</strong>.<br><br>"
            f"Regards,<br>GoldStandard Wealth Pvt Ltd"
        )
    else:
        body_html = body_html.replace('\n', '<br>')
    body_html += (
        "<br><br><p style='color:#888;font-size:11px'>"
        "Generated by Keystone &#8212; GoldStandard Wealth Pvt Ltd</p>"
    )

    token = ingestor._get_token()
    # Use custom sender mailbox if specified, else default
    mailbox = send_from or cfg["mailbox"]
    import requests as _req
    r = _req.post(
        f"https://graph.microsoft.com/v1.0/users/{mailbox}/sendMail",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body_html},
                "toRecipients": [{"emailAddress": {"address": a}} for a in recipients],
                "attachments": attachments,
            },
            "saveToSentItems": True,
        },
        timeout=30,
    )
    if r.status_code not in (200, 202):
        raise RuntimeError(f"Graph API {r.status_code}: {r.text[:300]}")


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Upload 0096 file to WealthSpectrum")
    ap.add_argument("file", help="Path to the 0096 XLS file")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not upload_creds_configured():
        raise SystemExit("FINCRM_USER / FINCRM_PASS (or FINCRM_UPLOAD_*) not set")

    def _cb(stage, detail):
        print(f"  [{stage:10s}] {detail}")

    result = upload_0096(args.file, progress_cb=_cb)
    print(f"\n{'OK' if result.ok else 'FAIL'}: {result.message}")
    if result.detail:
        print(f"Detail: {result.detail[:300]}")
