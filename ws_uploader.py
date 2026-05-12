# ws_uploader.py
# ─────────────────────────────────────────────────────────────────────────────
# WealthSpectrum file uploader — handles multi-step form submissions.
#
# Reuses the AES-128-CBC login from ws_downloader.py.
#
# Environment variables:
#   FINCRM_USER / FINCRM_PASS — WS portal credentials (used for both
#       downloads and uploads — single account)
#   FINCRM_URL = https://app.thegoldstandard.in/fincrm  (default)
# ─────────────────────────────────────────────────────────────────────────────

import os
import re
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import requests as _requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


# ── Optional request/response tracing ────────────────────────────────────────

def _trace_enabled() -> bool:
    return os.environ.get("KEYSTONE_WS_UPLOAD_TRACE", "").strip() in ("1", "true", "yes")


def _trace_dir() -> Path:
    base = os.environ.get("KEYSTONE_DATA_DIR") or str(Path(__file__).parent)
    from datetime import datetime as _dt
    d = Path(base) / "data" / "ws_upload_trace" / _dt.now().strftime("%Y%m%d_%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _trace_write(trace_dir: Path, name: str, kind: str, payload) -> None:
    """Dump a request or response payload to disk for later diff.

    name: short identifier like 'step1', 'step2', 'step3a'
    kind: 'req' or 'resp'
    payload: a PreparedRequest or a Response
    """
    try:
        if kind == "req":
            head = [
                f"{payload.method} {payload.url}",
                *(f"{k}: {v}" for k, v in payload.headers.items()),
                "",
            ]
            header_bytes = ("\n".join(head)).encode("utf-8", errors="replace")
            body = payload.body
            if body is None:
                body_bytes = b""
            elif isinstance(body, (bytes, bytearray)):
                body_bytes = bytes(body)
            else:
                body_bytes = str(body).encode("utf-8", errors="replace")
            (trace_dir / f"{name}_req.bin").write_bytes(header_bytes + body_bytes)
        else:  # resp
            head = [
                f"HTTP {payload.status_code} {payload.reason}",
                f"URL: {payload.url}",
                *(f"{k}: {v}" for k, v in payload.headers.items()),
                "",
            ]
            header_bytes = ("\n".join(head)).encode("utf-8", errors="replace")
            body_bytes = payload.content if payload.content is not None else b""
            (trace_dir / f"{name}_resp.bin").write_bytes(header_bytes + body_bytes)
    except Exception as _te:
        log.warning(f"_trace_write failed for {name}/{kind}: {_te}")


# ── Credentials ──────────────────────────────────────────────────────────────

def _cfg_file(name: str) -> Path:
    """Resolve a config file path. Honors KEYSTONE_CONFIG_DIR env var so UI
    edits on Azure (which land in /home/keystone-config) are seen by loaders.
    Falls back to the bundled config/ dir for local dev."""
    _cfg_dir = os.environ.get("KEYSTONE_CONFIG_DIR")
    if _cfg_dir:
        return Path(_cfg_dir) / name
    return Path(__file__).parent / "config" / name


def _base_url() -> str:
    return os.environ.get("FINCRM_URL", "https://app.thegoldstandard.in/fincrm").rstrip("/")

def _upload_user() -> str:
    return os.environ.get("FINCRM_USER", "")

def _upload_pass() -> str:
    return os.environ.get("FINCRM_PASS", "")

def upload_creds_configured() -> bool:
    return bool(_upload_user() and _upload_pass())


# Separate WS account used for the Apply Corp Actions step. The portal
# forbids the same user from running both the upload + the apply within a
# session, so this account stays distinct from FINCRM_USER. Set
# FINCRM_USER2 / FINCRM_PASS2 on Azure App Settings.
def _apply_user() -> str:
    return os.environ.get("FINCRM_USER2", "")

def _apply_pass() -> str:
    return os.environ.get("FINCRM_PASS2", "")

def apply_creds_configured() -> bool:
    return bool(_apply_user() and _apply_pass())


def _ist_naive_now():
    """Wall-clock in IST as a NAIVE datetime — matches what the WS
    portal renders in genericWait.jsp's status table.

    All `submit_dt` baselines in the bridges go through this helper so
    poll comparisons stay consistent regardless of host TZ. Azure App
    Service Linux containers are UTC by default, so a raw
    datetime.now() would be off by 5h30m and confuse the
    "skip rows older than submit by 90s" filter.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    _IST = _tz(_td(hours=5, minutes=30))
    return _dt.now(_IST).replace(tzinfo=None)


# ── Login (reuse ws_downloader's AES login) ─────────────────────────────────

def _login_upload(session: _requests.Session, auth_cache: Path) -> None:
    """Log in to the WS portal. Single credential set (FINCRM_USER /
    FINCRM_PASS) is used for both downloads and uploads — no env-var
    swap needed."""
    from ws_downloader import _login
    _login(session, auth_cache)


# ── Result type ──────────────────────────────────────────────────────────────

@dataclass
class UploadResult:
    ok: bool
    message: str
    detail: str = ""
    posting_id: str = ""

    def to_dict(self):
        return {"ok": self.ok, "message": self.message, "detail": self.detail,
                "posting_id": self.posting_id}


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


# Hard-error markers that mean the WS response is genuinely broken
# even though Tomcat returned HTTP 200. WS routinely renders login,
# stack traces, and validation banners at 200, so r.raise_for_status()
# alone is insufficient. Use this everywhere the bridge consumes a
# form-submit response and would otherwise trust the status code.
_WS_HARD_ERROR_MARKERS = (
    'session has expired',
    'exception',
    'stack trace',
    'name="j_username"',  # login form re-rendered → session destroyed
    'name="j_password"',
    'id="loginForm"',
)


def _detect_ws_hard_error(html: str) -> str:
    """Return a hard-error marker if `html` looks like a WS error / login
    page despite the response being HTTP 200; '' otherwise.

    Caller decides whether to surface as failure or attempt re-login.
    """
    if not html:
        return ''
    body_lc = html.lower()
    for m in _WS_HARD_ERROR_MARKERS:
        if m in body_lc:
            return m
    return ''


def _assert_ws_response_ok(response, *, context: str = ''):
    """Raise RuntimeError if a WS response is not actually OK.

    Combines `raise_for_status` (HTTP 4xx/5xx) with a body scan for
    Tomcat-200 error pages (login form re-render, stack trace, session
    expired, etc.). Bridges that wrap form submissions with this catch
    silent failures the previous bare `raise_for_status()` missed.
    """
    response.raise_for_status()
    marker = _detect_ws_hard_error(response.text or '')
    if marker:
        prefix = f'[{context}] ' if context else ''
        raise RuntimeError(
            f'{prefix}WS returned HTTP {response.status_code} but body '
            f'contains hard-error marker {marker!r} '
            f'(URL: {response.url})')


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

# Block Deals upload — 5-step flow observed in browser HAR:
#   1. GET  redirect.do?target=queryTradePosting&blockflag=Y&...      (file picker)
#   2. POST redirect.do?target=queryTradePostingMap&...   (multipart file)
#   3. POST queryTradePosting.do  mode=checkDuplicateFile
#   4. POST protected/withMenu.jsp?self=genericWait.jsp  mode=errorPage  (execute intent)
#   5. POST genericExeWait.do  (actually runs the mapper; returns result HTML)
UPLOAD_FORM_URL = "redirect.do?target=queryTradePosting&blockflag=Y&scope=*&cmScope=*&consolidation=C&srcMenuId=1448"
UPLOAD_MAP_PAGE = "redirect.do?target=queryTradePostingMap&blockflag=Y&menuDisp=Y&srcMenuId=1448"
WAIT_PAGE_URL   = "protected/withMenu.jsp?self=genericWait.jsp"
EXEC_WAIT_URL   = "genericExeWait.do"
MAP_ID_0096 = "96"
MAPPER_SOURCE_0096 = "spectrum_blocktrades_map"
MAPPER_FORMAT_0096 = "dd/MM/yyyy"
MAPPER_SCOPE_ATTR  = "TradePostingProcessLog"

# NSDL Contract Notes upload — replaces the legacy 0096 block-deals upload.
# Same Trade Posting URL + flow as 0096, only the mapid differs. WS's
# server looks up the source/format from the mapid on its side when these
# are populated by the browser's JS when the operator picks mapid=195
# from the picklist. Our script bypasses the picker, so we hard-code the
# values the WS mapid picklist associates with 195:
#   mapid  = 195  ("Common Contract Note Block Deal - NSDL Steady")
#   source = comm_contract_map
#   format = dd/MM/yyyy
# Leaving either blank produces a server-side NullPointerException at
# the mapper stage ("0 of 0 records posted, parsing_errors: 1").
MAP_ID_NSDL = "195"
MAPPER_SOURCE_NSDL = "comm_contract_map"
MAPPER_FORMAT_NSDL = "dd/MM/yyyy"


def upload_nsdl(file_path: str, progress_cb=None,
                 auth_cache_path: str = '') -> UploadResult:
    """Upload an NSDL SRK317CNSTAT contract notes file to WealthSpectrum.

    Same Trade Posting flow as the legacy 0096 block-deals upload, but
    with mapid=195. WS's mapper side derives source/format from the
    mapid (we leave them blank), so the body is otherwise identical.
    """
    return upload_0096(
        file_path,
        progress_cb=progress_cb,
        auth_cache_path=auth_cache_path,
        mapid=MAP_ID_NSDL,
        mapper_source=MAPPER_SOURCE_NSDL,
        mapper_format=MAPPER_FORMAT_NSDL,
        file_label='NSDL',
    )


def upload_0096(file_path: str, progress_cb=None,
                auth_cache_path: str = '',
                mapid: str = None,
                mapper_source: str = None,
                mapper_format: str = None,
                file_label: str = '0096') -> UploadResult:
    """
    Upload a 0096 XLS file to WealthSpectrum.

    Three-step flow:
      1. GET  upload form           → scrape CSRF + form action
      2. POST file (multipart)      → server stores temp file, returns Trade Posting page
      3. POST with the configured mapid → duplicate check + execute

    The function defaults to the legacy 0096 settings (mapid=96,
    source=spectrum_blocktrades_map, format=dd/MM/yyyy). Pass explicit
    values to drive other mappers — e.g. mapid=195 for the NSDL CN
    upload that replaces 0096. `upload_nsdl()` is a thin wrapper that
    supplies the NSDL parameters.

    progress_cb(stage, detail): optional callback for UI progress updates.

    When KEYSTONE_WS_UPLOAD_TRACE=1 is set, every request+response of
    this function is dumped under data/ws_upload_trace/<ts>/ so we can
    diff it byte-for-byte against a browser HAR capture.
    """
    # Defaults — keep backwards-compatible 0096 behaviour
    if mapid is None:         mapid         = MAP_ID_0096
    if mapper_source is None: mapper_source = MAPPER_SOURCE_0096
    if mapper_format is None: mapper_format = MAPPER_FORMAT_0096

    fpath = Path(file_path)
    if not fpath.exists():
        return UploadResult(False, f"File not found: {fpath}")

    trace_dir = _trace_dir() if _trace_enabled() else None
    if trace_dir:
        log.info(f"WS upload trace enabled → {trace_dir}")

    base = _base_url()
    # Auth cache location: explicit param > KEYSTONE_DATA_DIR env var >
    # bundled config dir (local dev fallback). On Azure we want this under
    # /home/keystone-data/config so it survives deploys.
    if auth_cache_path:
        auth_cache = Path(auth_cache_path)
    else:
        _data_dir = os.environ.get("KEYSTONE_DATA_DIR")
        if _data_dir:
            auth_cache = Path(_data_dir) / "config" / "ws_upload_auth.json"
        else:
            auth_cache = Path(__file__).parent / "config" / "ws_upload_auth.json"
    auth_cache.parent.mkdir(parents=True, exist_ok=True)

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
        _req1 = _requests.Request("GET", form_url, headers={"Referer": f"{base}/"})
        _prep1 = session.prepare_request(_req1)
        if trace_dir: _trace_write(trace_dir, "step1", "req", _prep1)
        r1 = session.send(_prep1, timeout=30)
        if trace_dir: _trace_write(trace_dir, "step1", "resp", r1)
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
    log.info(f"Step 1 OK — action={action1}, fields={list(hidden1.keys())}, values={hidden1}")
    # Dump the raw step-1 form HTML so we can see what fields the browser
    # would have access to (and any JS that might inject more at submit).
    _dump_dir = Path(os.environ.get("KEYSTONE_DATA_DIR") or
                     str(Path(__file__).parent)) / "data" / "ws_upload_probe"
    _dump_dir.mkdir(parents=True, exist_ok=True)
    (_dump_dir / "step1_form.html").write_text(r1.text, encoding="utf-8", errors="replace")

    # ── Step 2: POST the file ────────────────────────────────────────────
    if progress_cb:
        progress_cb("upload", f"Uploading {fpath.name}...")
    post_url = f"{base}/{action1}" if not action1.startswith("http") else action1
    # MIME type by extension — xlsx vs xls
    _ext = fpath.suffix.lower()
    if _ext == ".xlsx":
        _mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        _mime = "application/vnd.ms-excel"

    # Build the multipart request manually via PreparedRequest so we can
    # capture and dump the exact bytes sent over the wire — this is how we
    # diff our request against what the browser sends when the WS portal
    # accepts the same file directly.
    try:
        with open(fpath, "rb") as f:
            files = {"filepath": (fpath.name, f, _mime)}
            req = _requests.Request(
                "POST", post_url,
                data=hidden1, files=files,
                headers={"Referer": form_url},
            )
            prepped = session.prepare_request(req)
            if trace_dir: _trace_write(trace_dir, "step2", "req", prepped)
            r2 = session.send(prepped, timeout=120)
            if trace_dir: _trace_write(trace_dir, "step2", "resp", r2)
        r2.raise_for_status()
        (_dump_dir / "step2_response.html").write_text(
            r2.text, encoding="utf-8", errors="replace")
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

    # Pre-build the mapper field bag from the scraped step-2 response.
    # The page's JS would normally populate source/file/format when the
    # operator picks mapid=96 from the autocomplete; we inject them
    # directly. Overwrite (not setdefault) because these keys exist
    # but are empty in the scraped form.
    def _mapper_fields(fields: dict, mode: str, action_str: str,
                       include_buttons: bool) -> dict:
        # Start from scraped fields but drop UI-only controls that
        # browsers don't submit (checkboxes that are unchecked, etc.)
        out = {k: v for k, v in fields.items() if k != "brkchgFlag"}
        out["mapid"]           = mapid
        out["mode"]            = mode
        out["actionString"]    = action_str
        out["source"]          = mapper_source
        out["format"]          = mapper_format
        out["refreshContent"]  = "call"
        out["blockflag"]       = "Y"
        out["srcMenuId"]       = "1448"
        if not out.get("firmid"):
            out["firmid"] = "0"
        tf = out.get("tempfile") or fpath.name
        out["file"] = tf
        if include_buttons:
            out["temp"]  = "Submit"
            out["reset"] = "Reset"
        else:
            out.pop("temp", None)
            out.pop("reset", None)
        return out

    map_page_url = f"{base}/{UPLOAD_MAP_PAGE}"
    wait_page_url = f"{base}/{WAIT_PAGE_URL}"

    # ── Step 3a: Duplicate check (mode=checkDuplicateFile) ───────────────
    if progress_cb:
        progress_cb("checking", "Checking for duplicates...")

    dup_check_fields = _mapper_fields(fields2, "checkDuplicateFile", "",
                                       include_buttons=True)
    qtp_url = f"{base}/queryTradePosting.do"
    try:
        _req3a = _requests.Request("POST", qtp_url, data=dup_check_fields,
                                   headers={"Referer": map_page_url})
        _prep3a = session.prepare_request(_req3a)
        if trace_dir: _trace_write(trace_dir, "step3a", "req", _prep3a)
        r3a = session.send(_prep3a, timeout=60)
        if trace_dir: _trace_write(trace_dir, "step3a", "resp", r3a)
        r3a.raise_for_status()
    except Exception as e:
        return UploadResult(False, f"Duplicate check failed: {e}")

    # actionString comes back in the scraped response
    dup_fields = _scrape_all_inputs(r3a.text)
    action_str = dup_fields.get("actionString", "")
    if not action_str:
        m = re.search(r'name=["\']actionString["\'].*?value=["\']([^"\']*)', r3a.text)
        if m: action_str = m.group(1)
    log.info(f"Step 3a — actionString={action_str!r}")

    if action_str == "nextWithCancel":
        return UploadResult(False,
                            "WS rejected: file already uploaded and duplicates are not allowed for this map")

    # Echo the server's actionString verbatim. The two well-known values:
    #   "next"               — fresh upload
    #   "nextWithOKContinue" — duplicate but operator overrode
    # Substituting "nextWithOKContinue" when the server returned "next"
    # would falsely tell WS the operator clicked OK on a duplicate
    # warning that was never shown.
    if action_str not in ("next", "nextWithOKContinue"):
        log.warning(f"upload_0096 — unexpected actionString {action_str!r}; "
                    f"aborting before kick-off")
        return UploadResult(False,
                            f"unrecognised actionString from WS: {action_str!r}")
    effective_action = action_str

    # ── Step 3b: Run the mapper (the actual posting) ─────────────────────
    # The page's setExecWaitAction() function does TWO things in sequence:
    #   1. retrieveURLForExeNWait('/fincrm/queryTradePosting.do', ...) — a
    #      synchronous XHR with mode=errorPage that runs the mapper.
    #   2. document.forms[0].submit() — navigates the user's tab to the
    #      wait page UI for visual feedback while the job runs.
    # The HAR doesn't surface the XHR as a top-level entry but it's
    # mandatory — without it the wait page loads but no job is queued.
    if progress_cb:
        progress_cb("posting", "Running mapper...")

    exec_intent_fields = _mapper_fields(fields2, "errorPage", effective_action,
                                         include_buttons=False)
    try:
        _req3b = _requests.Request("POST", qtp_url, data=exec_intent_fields,
                                   headers={"Referer": map_page_url})
        _prep3b = session.prepare_request(_req3b)
        if trace_dir: _trace_write(trace_dir, "step3b", "req", _prep3b)
        r3b = session.send(_prep3b, timeout=180)
        if trace_dir: _trace_write(trace_dir, "step3b", "resp", r3b)
        r3b.raise_for_status()
    except Exception as e:
        return UploadResult(False, f"Mapper run failed: {e}")
    log.info(f"Step 3b OK — mapper response ({len(r3b.text)} bytes)")

    # Step 3b's response IS the posting result — the browser's wait page
    # UI / polls / genericExeWait.do are visual fluff that just refetch
    # the same data. Parse counts directly from r3b.
    counts = _parse_posting_result(r3b.text)
    log.info(f"Step 3b — counts: {counts}")

    # Save for debugging (non-trace mode)
    dump_dir = Path(__file__).parent / "data" / "ws_upload_probe"
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

    # True duplicate carries a real posting_id (non-zero integer string).
    # A false success — WS mapper crashed before any row was posted — has
    # posting_id='0' or empty AND a NullPointerException in error_details.
    is_true_duplicate = (
        was_dup
        and total == 0
        and processed == 0
        and posting_id
        and posting_id != "0"
    )
    if is_true_duplicate:
        return UploadResult(
            True,
            f"Re-upload: 0 new records (file was already posted) — Posting ID: {posting_id}",
            "duplicate",
            posting_id=posting_id,
        )

    # False success: WS hit a mapper exception and posted nothing. Surface
    # the real error to the caller instead of silently claiming duplicate.
    if was_dup and total == 0 and processed == 0:
        return UploadResult(
            False,
            f"WS rejected the file (0 rows processed). Mapper error: {error_det[:200]}",
            error_det,
        )

    # WS's posting summary counts the 0096 file's header row as both a
    # "total record" and a "parsing error" on every upload. That baseline
    # is noise for the operator — adjust the total and parse_err for
    # display so the UI reads "20 of 20 records posted" instead of the
    # misleading "20 of 21 records posted (1 parsing error)".
    HEADER_ROWS = 1
    disp_total  = max(0, total - HEADER_ROWS) if total > 0 else total
    disp_parse  = max(0, parse_err - HEADER_ROWS) if parse_err > 0 else parse_err

    # Partial success: some records posted, some had row-level errors.
    # WS still considers this a successful upload — the bad rows are
    # reported but the good ones are inserted. Report ok=True with
    # a note about the per-row errors.
    if processed > 0:
        msg = f"{processed} of {disp_total} records posted"
        if val_err > 0 or disp_parse > 0:
            parts = []
            if val_err > 0:   parts.append(f"{val_err} validation")
            if disp_parse > 0: parts.append(f"{disp_parse} parsing")
            msg += f" ({', '.join(parts)} error{'s' if (val_err+disp_parse)>1 else ''})"
        if posting_id:
            msg += f" — Posting ID: {posting_id}"
        return UploadResult(True, msg, error_det, posting_id=posting_id)

    # Hard failure: nothing posted, only errors
    if val_err > 0 or disp_parse > 0:
        msg = (f"WS rejected the file: 0 processed, "
               f"{val_err} validation errors, {disp_parse} parsing errors")
        return UploadResult(False, msg, error_det)

    # Empty result with no errors — unusual; treat as soft success
    msg = f"0 of {disp_total} records posted (no errors reported)"
    if posting_id:
        msg += f" — Posting ID: {posting_id}"
    return UploadResult(True, msg, "", posting_id=posting_id)


# ── Block-Trade Allocation ───────────────────────────────────────────────────

BLOCK_TRADE_POSTING_URL = "redirect.do?target=blocktradePosting&scope=*&cmScope=*&consolidation=C&srcMenuId=1449"
VIEW_TRADE_BLOCKING_URL = "viewTradeBlocking.do"


@dataclass
class AllocationResult:
    ok: bool
    message: str

    def to_dict(self):
        return {"ok": self.ok, "message": self.message}


def allocate_block_trades(posting_id: str, session: _requests.Session = None,
                          auth_cache_path: str = '',
                          progress_cb=None) -> AllocationResult:
    """Post-upload step: open blocktradePosting, seed the posting ID,
    submit, verify no 'Sel Clients' in Block Ref # (order mismatch),
    then click Allocate and confirm all Block Ref # cells turn green/numeric.

    Uses the same upload session/creds as upload_0096.
    """
    if not posting_id or posting_id == "0":
        return AllocationResult(False, "No posting ID to allocate")

    base = _base_url()

    owns_session = session is None
    if owns_session:
        session = _requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Referer": f"{base}/",
        })
        if auth_cache_path:
            auth_cache = Path(auth_cache_path)
        else:
            _data_dir = os.environ.get("KEYSTONE_DATA_DIR")
            if _data_dir:
                auth_cache = Path(_data_dir) / "config" / "ws_upload_auth.json"
            else:
                auth_cache = Path(__file__).parent / "config" / "ws_upload_auth.json"
        auth_cache.parent.mkdir(parents=True, exist_ok=True)
        try:
            _login_upload(session, auth_cache)
        except Exception as e:
            return AllocationResult(False, f"Login failed: {e}")

    # ── Step 1: GET the blocktradePosting page ──────────────────────────
    if progress_cb:
        progress_cb("allocate", "Opening block-trade posting page...")
    btp_url = f"{base}/{BLOCK_TRADE_POSTING_URL}"
    try:
        r1 = session.get(btp_url, timeout=30)
        r1.raise_for_status()
    except Exception as e:
        return AllocationResult(False, f"Failed to load block-trade posting page: {e}")

    # Scrape hidden fields from the form
    fields = _scrape_all_inputs(r1.text)

    # ── Step 2: Seed the posting ID and submit the form ─────────────────
    # The first field on the form accepts the posting ID. From the page
    # structure it's typically named "postingId" or similar. We also need
    # the CSRF token. The AJAX call that seeds the posting ID hits the
    # same form action — we replicate by POSTing with the ID value.
    if progress_cb:
        progress_cb("allocate", f"Submitting posting ID {posting_id}...")

    # Identify the posting ID field. The UI label is "Posting id" but the
    # HTML input name may differ. Try exact names first, then find by
    # adjacent label text, then fall back to the first text input.
    log.info("allocate_block_trades: scraped fields: %s", list(fields.keys()))
    pid_field = None
    for candidate in ("postingId", "blockPostingId", "posting_id", "postingid",
                       "Posting id", "Postingid", "PostingId"):
        if candidate in fields:
            pid_field = candidate
            break
    if not pid_field:
        # Search by partial match (case-insensitive)
        for k in fields:
            if "posting" in k.lower():
                pid_field = k
                break
    if not pid_field:
        # Look for an input near a label containing "posting"
        soup1 = BeautifulSoup(r1.text, "html.parser")
        for label in soup1.find_all(["label", "td", "th"]):
            if "posting" in (label.get_text(strip=True) or "").lower():
                inp = label.find_next("input")
                if inp and inp.get("name"):
                    pid_field = inp["name"]
                    break
    if not pid_field:
        # Last resort: first visible text input
        soup1 = soup1 if 'soup1' in dir() else BeautifulSoup(r1.text, "html.parser")
        first_text = soup1.find("input", {"type": ["text", "", None]})
        if first_text and first_text.get("name"):
            pid_field = first_text["name"]
    if not pid_field:
        _dump = Path(os.environ.get("KEYSTONE_DATA_DIR") or
                     str(Path(__file__).parent)) / "data" / "ws_upload_probe"
        _dump.mkdir(parents=True, exist_ok=True)
        (_dump / "block_trade_posting_page.html").write_text(
            r1.text, encoding="utf-8", errors="replace")
        return AllocationResult(False,
                                f"Could not find posting ID field on block-trade page. "
                                f"Scraped fields: {list(fields.keys())}. Page saved.")
    log.info("allocate_block_trades: using field %r for posting ID", pid_field)

    fields[pid_field] = posting_id
    vtb_url = f"{base}/{VIEW_TRADE_BLOCKING_URL}"
    try:
        r2 = session.post(vtb_url, data=fields,
                          headers={"Referer": btp_url}, timeout=30)
        r2.raise_for_status()
    except Exception as e:
        return AllocationResult(False, f"Block-trade submit failed: {e}")

    # Always save viewTradeBlocking response for debugging
    _save_debug_page(r2.text, "block_trade_result.html")

    # ── Step 3: Find the data table and check for order mismatches ──────
    if progress_cb:
        progress_cb("allocate", "Checking for order mismatches...")

    soup2 = BeautifulSoup(r2.text, "html.parser")
    data_tbl, block_ref_col = _find_data_table(soup2)

    if data_tbl is None:
        return AllocationResult(False,
                                "Could not find the trade data table (expected header row "
                                "with 'Srl' and 'Block Ref#'). Page saved for debugging.")

    data_rows = _extract_data_rows(data_tbl, block_ref_col)
    log.info("allocate_block_trades: %d data rows: %s",
             len(data_rows),
             [{"srl": r["srl"], "ref": r["block_ref"], "method": r["method"]}
              for r in data_rows])

    # Fail closed: allocation is only safe when EVERY row is explicitly
    # "Based on Orders". Anything else — including "Sel.Clients",
    # "Sel Clients", an empty string (row wasn't parsed cleanly), or an
    # unexpected method label — aborts. Previous version did a loose
    # `"Sel" in method` check, which silently passed blank/mis-parsed
    # rows and let partial allocations reach auth/post (seen on the
    # 2026-04-21 run).
    _OK_METHOD = "based on orders"
    bad_rows = [
        r for r in data_rows
        if (r.get("method") or "").strip().lower() != _OK_METHOD
    ]
    if bad_rows:
        # Group bad rows by method label so the summary is "Sel.Clients: 4"
        # instead of "1=Sel.Clients, 1=Sel.Clients, 2=Sel.Clients, ...".
        from collections import Counter
        method_counts = Counter(
            (r.get("method") or "").strip() or "(blank)" for r in bad_rows
        )
        breakdown = ", ".join(
            f"{m}: {c}" for m, c in method_counts.most_common()
        )

        # Surface any row-level error messages (the red-font line WS
        # puts in the combined "STP Status / Error Msg. / Remarks"
        # column). De-dup and cap length so we never blow up the
        # status line.
        #
        # NOTE: STP Status is intentionally NOT surfaced — on an
        # unallocated block trade it permanently reads "Un Match CMIS"
        # and mistaking it for an error signal just adds noise.
        extra_bits = []
        err_msgs = sorted({(r.get("error_msg") or "").strip()
                           for r in data_rows
                           if (r.get("error_msg") or "").strip()})
        if err_msgs:
            extra_bits.append("WS errors: " + ("; ".join(err_msgs))[:240])

        detail = f"{len(bad_rows)} of {len(data_rows)} rows not 'Based on Orders' ({breakdown})"
        if extra_bits:
            detail += ". " + ". ".join(extra_bits)
        return AllocationResult(
            False,
            f"Order mismatch: {detail}. "
            f"Trades will NOT be allocated; fix the orders in WS and retry.")

    # Second guard: even when every row is "Based on Orders", WS can
    # still flag a row with a red-font error message ("Invalid Broker",
    # etc.). Surface those and abort before allocation — the
    # allocation call against those rows would either silently fail or
    # succeed with bad data.
    err_msgs_any = sorted({(r.get("error_msg") or "").strip()
                            for r in data_rows
                            if (r.get("error_msg") or "").strip()})
    if err_msgs_any:
        joined = ("; ".join(err_msgs_any))[:240]
        return AllocationResult(
            False,
            f"WS flagged row-level errors on the block-trade page: {joined}. "
            f"Trades will NOT be allocated; resolve in WS and retry.")

    # ── Step 4: Allocate — replicate javascript:AllOrderClients() ──────
    # AllOrderClients() typically sets a hidden field (actionid or mode)
    # and submits the page form back to viewTradeBlocking.do. We scrape
    # the form from the viewTradeBlocking response, set the allocation
    # action, and POST.
    if progress_cb:
        progress_cb("allocate", "Allocating trades...")

    # AllOrderClients() JS sets mode='allorderclients', sent='submit',
    # then submits the form to viewTradeBlocking.do. Replicate that.
    r2_fields = _scrape_all_inputs(r2.text)
    r2_fields["mode"] = "allorderclients"
    r2_fields["sent"] = "submit"
    r2_fields["lastAjaxForTrade"] = "allorderclients"
    log.info("allocate_block_trades: POSTing allocation with mode=allorderclients")

    try:
        r3 = session.post(vtb_url, data=r2_fields,
                          headers={"Referer": vtb_url}, timeout=60)
        r3.raise_for_status()
    except Exception as e:
        return AllocationResult(False, f"Allocate request failed: {e}")

    _save_debug_page(r3.text, "block_trade_allocate_result.html")

    # ── Step 5: Verify allocation succeeded ─────────────────────────────
    if progress_cb:
        progress_cb("allocate", "Verifying allocation...")

    soup3 = BeautifulSoup(r3.text, "html.parser")
    data_tbl3, block_ref_col3 = _find_data_table(soup3)

    allocated_row_count = 0
    if data_tbl3 is not None:
        post_rows = _extract_data_rows(data_tbl3, block_ref_col3)
        log.info("allocate_block_trades: post-allocate Block Ref# values: %s",
                 [r["block_ref"] for r in post_rows])

        if post_rows:
            all_numeric = all(re.match(r'^\d+$', r["block_ref"]) for r in post_rows)
            if not all_numeric:
                bad = [r["block_ref"] for r in post_rows
                       if not re.match(r'^\d+$', r["block_ref"])]
                return AllocationResult(False,
                                        f"Allocation may have failed — non-numeric "
                                        f"Block Ref# values: {bad}. Page saved for debugging.")
            allocated_row_count = len(post_rows)

    if "error" in r3.text.lower() and "exception" in r3.text.lower():
        return AllocationResult(False, "Allocation page returned an error")

    # ── Step 6: Authorise All and Post ──────────────────────────────────
    # Replicates the authorizePost() JS function which POSTs to
    # viewTradePosting.do?mode=authorisepost&sent=submit&postingId=X
    # &backpostingid=Y. Success is "moved to the next screen" — the
    # response is not an error page.
    if progress_cb:
        progress_cb("allocate", "Authorising and posting...")

    posting_id_to_auth = posting_id
    back_posting_id    = posting_id
    m = re.search(r"authorizePost\s*\(\s*'?(\d+)'?\s*,\s*'?(\d+)'?\s*\)",
                  r3.text)
    if m:
        posting_id_to_auth = m.group(1)
        back_posting_id    = m.group(2)
        log.info("authorizePost params from page: postingId=%s backpostingid=%s",
                 posting_id_to_auth, back_posting_id)

    auth_url = (
        f"{base}/viewTradePosting.do"
        f"?mode=authorisepost&sent=submit"
        f"&postingId={posting_id_to_auth}"
        f"&backpostingid={back_posting_id}"
    )
    try:
        r4 = session.post(auth_url,
                          headers={"Referer": vtb_url}, timeout=60)
        r4.raise_for_status()
    except Exception as e:
        return AllocationResult(False, f"Authorise-and-post failed: {e}")

    _save_debug_page(r4.text, "block_trade_auth_post_result.html")

    # Verification: require a positive success marker and scan for a
    # broader set of failure markers. Previous version did
    #   ("error" in low AND "exception" in low) OR "failed" in low[:2000]
    # which silently passed WS responses that said e.g. "Some trades
    # could not be authorized" — observed on the 2026-04-21 run.
    #
    # Ground truth for success comes from the saved debug page
    # block_trade_auth_post_result.html: on a successful auth/post WS
    # redirects to the Trade Posting stage, and the response contains
    # a <TD class=heading>Trade Posting</TD>, a form named
    # viewTradePostingForm, the URL viewTradePosting.do, and a hidden
    # `postingid` input. Any one of those is enough to distinguish
    # from a stayed-on-BlockTrade-page failure response.
    low = r4.text.lower()
    FAILURE_MARKERS = (
        # Generic WS failure language
        "could not be authorised", "could not be authorized",
        "could not be posted", "cannot be posted", "cannot be authorized",
        "not all trades", "some trades could not", "posting failed",
        "authorisation failed", "authorization failed",
        # Lower-level stack markers — the old check required both
        # "error" and "exception"; WS sometimes prints only one.
        "stacktrace", "null pointer",
    )
    SUCCESS_MARKERS = (
        # WS's auth/post redirects to the Trade Posting stage. These
        # four markers ALL appear in that response (verified against
        # block_trade_auth_post_result.html from a known-good run).
        "viewtradeposting.do",
        "viewtradepostingform",
        '>trade posting</td>',
        'name="postingid"',
    )

    hit_failure = any(m in low for m in FAILURE_MARKERS)
    hit_success = any(m in low for m in SUCCESS_MARKERS)

    if hit_failure:
        return AllocationResult(
            False,
            "Authorise-and-post returned a failure page — WS indicated one or "
            "more trades could not be authorised/posted. No custody emails "
            "have been sent. Inspect block_trade_auth_post_result.html.")

    if not hit_success:
        # Fail closed: response doesn't explicitly confirm success.
        # Safer than optimistically assuming auth/post worked and
        # then sending custody emails.
        return AllocationResult(
            False,
            "Authorise-and-post response has no Trade Posting stage marker — "
            "refusing to proceed to custody dispatch. Inspect "
            "block_trade_auth_post_result.html to confirm WS status.")

    if allocated_row_count:
        return AllocationResult(True,
                                f"All {allocated_row_count} trades allocated, "
                                f"authorised and posted")
    return AllocationResult(True, "Trades allocated, authorised and posted")


def _find_data_table(soup: BeautifulSoup):
    """Find the trade data table by locating a <b>Srl</b> marker and then
    picking the ENCLOSING table that actually contains all the data rows.

    WS pages nest tables 3+ levels deep. The Srl-bold header lives in a
    sub-table that holds only a handful of data rows; the real row list
    sits inside a larger outer table that wraps every sub-table. Picking
    the innermost parent (old behaviour) would extract 2 of 20 rows,
    understate the mismatch count, and hide the real problem from the
    UI.

    Algorithm: find every ancestor <table> of a <b>Srl</b> cell, count
    the numeric-first-cell rows visible under each, and return the one
    with the MOST rows. Block Ref# column index is then picked from the
    first header row that lists both 'Srl' and 'Block Ref#'.

    Returns (table_element, block_ref_col_index) or (None, None).
    """
    candidates: list[tuple] = []
    for td in soup.find_all("td"):
        b = td.find("b")
        if not (b and b.get_text(strip=True) == "Srl"):
            continue
        for ancestor in td.find_parents("table"):
            data_count = 0
            for tr in ancestor.find_all("tr"):
                cells = tr.find_all("td", recursive=False)
                if cells and re.match(r'^\d+$', cells[0].get_text(strip=True)):
                    data_count += 1
            candidates.append((ancestor, data_count))
    if not candidates:
        return None, None
    # Unique tables (an ancestor may be shared across multiple Srl cells)
    # and pick the one with the most data rows.
    seen = set()
    best_tbl = None
    best_count = -1
    for tbl, cnt in candidates:
        if id(tbl) in seen:
            continue
        seen.add(id(tbl))
        if cnt > best_count:
            best_count = cnt
            best_tbl = tbl
    if best_tbl is None:
        return None, None
    # Find the Block Ref# column index from the first header row that
    # contains both 'Srl' and 'Block Ref#'.
    for tr in best_tbl.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 10:
            continue
        texts = [c.get_text(" ", strip=True) for c in cells]
        if "Srl" not in texts:
            continue
        for idx, t in enumerate(texts):
            if "Block Ref#" in t:
                return best_tbl, idx
    return best_tbl, None


def _extract_data_rows(table, block_ref_col: int) -> list:
    """Extract data rows from the trade table, skipping the header row.

    The Block Ref# cell contains hidden inputs, text, <br> tags, and <a>
    links mixed together. We extract the first standalone number from the
    cell's direct text nodes (ignoring input values and link text like
    "Based on Orders").

    WS's block-trade page uses nested tables, so the same row often shows
    up twice under BeautifulSoup's default recursive find_all. We dedup
    by (srl, block_ref) at the end so downstream messages show the true
    row count instead of 2× the real number.

    Returns list of dicts with keys:
      srl, block_ref, method, stp_status, error_msg
    """
    rows = []
    # Locate the STP Status + Error Msg columns once, by header text.
    # IMPORTANT: use recursive=False when pulling cells, otherwise nested
    # tables in WS's layout inflate the cell count and offset the column
    # indices (observed on 2026-04-21: stp/err misaligned to the srl col).
    stp_col = err_col = None
    for header_row in table.find_all("tr"):
        cells = header_row.find_all("td", recursive=False)
        if not cells or not any(c.get_text(strip=True) == "Srl" for c in cells):
            continue
        for idx, c in enumerate(cells):
            t = c.get_text(" ", strip=True).upper()
            if stp_col is None and "STP" in t and "STATUS" in t:
                stp_col = idx
            if err_col is None and "ERROR" in t and "MSG" in t:
                err_col = idx
        if stp_col is not None or err_col is not None:
            break
    for row in table.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if not cells or len(cells) <= block_ref_col:
            continue
        # Only include rows where first cell is a serial number
        first_text = cells[0].get_text(strip=True)
        if not re.match(r'^\d+$', first_text):
            continue
        ref_cell = cells[block_ref_col]
        ref_info = _extract_block_ref(ref_cell)
        def _clean(s: str) -> str:
            # Collapse any run of whitespace (incl. \n, \t) to a single space.
            return re.sub(r'\s+', ' ', s or '').strip()

        # WS stacks three values vertically inside a single
        # "STP Status / Error Msg. / Remarks" column:
        #   line 1 = STP Status    (always populated, e.g. "Un Match CMIS")
        #   line 2 = Error Msg     (red font, only when there's an error)
        #   line 3 = Remarks       (optional)
        # The error-msg line is wrapped in <font color="#FF0000"><b>…</b></font>;
        # we pull any such red-font text as error_msg and use the rest as
        # stp_status, so "Un Match CMIS" doesn't get misread as an error.
        stp_status = ""
        error_msg  = ""
        if stp_col is not None and stp_col == err_col:
            cell = cells[stp_col] if len(cells) > stp_col else None
            if cell is not None:
                red_nodes = cell.find_all(
                    "font",
                    attrs={"color": lambda v: isinstance(v, str) and v.upper().replace("#", "").startswith("FF0000")},
                )
                error_msg  = _clean(" ".join(
                    r.get_text(" ", strip=True) for r in red_nodes
                    if r.get_text(strip=True)
                ))
                # Build stp_status from the cell's text excluding the
                # red-font subtrees so we don't double-count the error.
                parts: list[str] = []
                def _walk(node):
                    for ch in node.children:
                        nm = getattr(ch, "name", None)
                        if nm == "font" and (ch.get("color") or "").upper().replace("#", "").startswith("FF0000"):
                            continue
                        if isinstance(ch, str):
                            parts.append(ch)
                        elif nm == "br":
                            parts.append(" ")
                        else:
                            _walk(ch)
                _walk(cell)
                stp_status = _clean("".join(parts))
        else:
            stp_status = (_clean(cells[stp_col].get_text(" ", strip=True))
                          if stp_col is not None and len(cells) > stp_col else "")
            error_msg  = (_clean(cells[err_col].get_text(" ", strip=True))
                          if err_col is not None and len(cells) > err_col else "")
        rows.append({
            "srl":        first_text,
            "block_ref":  ref_info["ref"],
            "method":     ref_info["method"],
            "stp_status": stp_status,
            "error_msg":  error_msg,
        })
    # Dedup — prefer the first occurrence so outer-table rows with a
    # filled STP/error column win over thin inner-table repeats.
    seen = set()
    deduped = []
    for r in rows:
        key = (r["srl"], r["block_ref"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped


def _extract_block_ref(td) -> dict:
    """Extract the Block Ref# value and allocation method from a complex <td>.

    The cell contains hidden inputs, a bare number (the ref), <br> tags,
    and an <a> link whose text is either "Based on Orders" (good) or
    "Sel.Clients" (order mismatch — trades can't be allocated).

    Returns {'ref': '105491', 'method': 'Based on Orders'} or
            {'ref': '105491', 'method': 'Sel.Clients'}.
    """
    # Extract the link text — this tells us the allocation method
    links = [a.get_text(strip=True) for a in td.find_all("a")]
    method = ""
    for link_text in links:
        if link_text in ("Based on Orders", "Sel.Clients", "Sel Clients"):
            method = link_text
            break

    # Extract the numeric ref from direct text nodes
    ref = ""
    for child in td.children:
        if isinstance(child, str):
            cleaned = child.strip()
            if re.match(r'^\d+$', cleaned):
                ref = cleaned
                break
    if not ref:
        full_text = td.get_text(" ", strip=True)
        m = re.search(r'\b(\d{4,})\b', full_text)
        if m:
            ref = m.group(1)

    return {"ref": ref, "method": method}


def _save_debug_page(html: str, filename: str) -> None:
    dump_dir = Path(os.environ.get("KEYSTONE_DATA_DIR") or
                    str(Path(__file__).parent)) / "data" / "ws_upload_probe"
    dump_dir.mkdir(parents=True, exist_ok=True)
    (dump_dir / filename).write_text(html, encoding="utf-8", errors="replace")


# ── Post-Trade-Recon Dispatch ─────────────────────────────────────────────────

@dataclass
class DispatchResult:
    ok: bool
    upload_result: UploadResult
    custodian_results: list  # [{custodian, file, email_ok, error}]
    allocation_result: AllocationResult | None = None

    def to_dict(self):
        d = {
            "ok": self.ok,
            "upload": self.upload_result.to_dict(),
            "custodians": self.custodian_results,
        }
        if self.allocation_result:
            d["allocation"] = self.allocation_result.to_dict()
        return d


def _load_dispatch_config() -> dict:
    """Load dispatch config — prefer data/ (survives deploys), fall back to config/."""
    import json
    data_path = Path(__file__).parent / "data" / "custodian_dispatch.json"
    if data_path.exists():
        try:
            return json.loads(data_path.read_text())
        except Exception:
            pass
    config_path = _cfg_file("custodian_dispatch.json")
    if config_path.exists():
        return json.loads(config_path.read_text())
    return {"custodians": {}}


def _involved_mapins_by_custodian(date_str: str, file_0096: str = '') -> dict:
    """Determine which MAPINs traded today, grouped by custodian.

    Reads the 0096 XLS file directly (column 17 = mapin_id) rather than
    depending on a sidecar JSON. Resolves broker pool_aliases so UCC-shaped
    codes (e.g. G587) map to the canonical pool mapin (GOLDETEPMS).

    Returns: {custodian: [{'mapin': ..., 'strategy': ...}, ...]}
    """
    import json
    hub_path = _cfg_file("pools_hub.json")
    if not hub_path.exists():
        return {}
    hub = json.loads(hub_path.read_text())
    mapin_to_cust = {}
    mapin_to_strategy = {}
    alias_to_canonical = {}
    pool_id_to_mapin = {}
    for pool in hub.get("pools", []):
        mapin = (pool.get("mapin") or "").strip()
        cust  = (pool.get("custodian_bank") or "").strip().upper()
        name  = (pool.get("strategy_name") or pool.get("pool_id") or mapin)
        if mapin and cust:
            mapin_to_cust[mapin.upper()] = cust
            mapin_to_strategy[mapin.upper()] = name
            alias_to_canonical[mapin.upper()] = mapin
        pid = (pool.get("pool_id") or "").strip()
        if pid and mapin:
            pool_id_to_mapin[pid] = mapin

    # Aliases now live on the broker side (broker.pool_aliases). Each
    # entry is {pool_id, alias_code, note}; we resolve pool_id → mapin
    # to keep alias_to_canonical in the same shape as before.
    bm_path = _cfg_file("broker_map.json")
    if bm_path.exists():
        try:
            bm = json.loads(bm_path.read_text())
            for broker in bm.get("brokers", []) or []:
                for alias in broker.get("pool_aliases", []) or []:
                    code = (alias.get("alias_code") or "").strip().upper()
                    pid  = (alias.get("pool_id") or "").strip()
                    canon = pool_id_to_mapin.get(pid)
                    if code and canon:
                        alias_to_canonical[code] = canon
        except Exception:
            pass

    # Find the 0096 file
    fpath = Path(file_0096) if file_0096 else None
    if fpath and not fpath.exists():
        fpath = None
    if fpath is None:
        _base = Path(os.environ.get("KEYSTONE_DATA_DIR") or str(Path(__file__).parent))
        out_dir = _base / "data" / date_str / "output"
        candidates = sorted(out_dir.glob("*0096*.xls"), reverse=True) if out_dir.exists() else []
        if candidates:
            fpath = candidates[0]

    if fpath is None or not fpath.exists():
        log.error("_involved_mapins_by_custodian: no 0096 file found for %s", date_str)
        return {}

    # Read MAPIN column (index 17, 0-based) from the 0096 XLS
    mapins_in_file: set = set()
    try:
        ext = str(fpath).lower().rsplit(".", 1)[-1]
        if ext == "xls":
            import xlrd
            wb = xlrd.open_workbook(str(fpath))
            ws = wb.sheet_by_index(0)
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
        log.error("_involved_mapins_by_custodian: failed to read 0096 file %s: %s", fpath, e)
        return {}

    if not mapins_in_file:
        log.info("0096 file has no MAPIN rows — no dispatch needed")
        return {}

    from collections import defaultdict
    result: dict = defaultdict(list)
    seen: set = set()
    for raw in mapins_in_file:
        canonical = alias_to_canonical.get(raw.upper(), raw)
        key = canonical.upper()
        if key in seen:
            continue
        c = mapin_to_cust.get(key)
        if c:
            seen.add(key)
            result[c].append({'mapin': canonical,
                              'strategy': mapin_to_strategy.get(key, canonical)})
        else:
            log.warning("_involved_mapins_by_custodian: mapin %r (from 0096 row %r) "
                        "not mapped to any custodian — skipping", canonical, raw)

    if not result:
        log.error("_involved_mapins_by_custodian: 0096 rows found but none resolved "
                  "to a configured custodian")
    return dict(result)


def dispatch_nsdl(nsdl_file: str, date_str: str,
                   progress_cb=None,
                   auth_cache_path: str = '') -> DispatchResult:
    """Full NSDL-based post-trade-recon automation.

    Mirrors dispatch_trades (0096 flow) but uploads the NSDL
    SRK317CNSTAT contract notes file via mapid=195. The custodian
    mapin list is still read from the 0096 file on disk (generated by
    the trade-recon engine earlier) — the NSDL file is a flat list of
    contract notes without pool/custodian routing metadata.
    """
    # Need the 0096 file for mapin → custodian lookup
    import os as _os
    _base = Path(_os.environ.get("KEYSTONE_DATA_DIR") or str(Path(__file__).parent))
    out_dir = _base / "data" / date_str / "output"
    zero096 = None
    if out_dir.exists():
        cands = list(out_dir.glob('0096_*.xlsx')) + list(out_dir.glob('0096_*.xls'))
        if cands:
            zero096 = max(cands, key=lambda p: p.stat().st_mtime)
    return dispatch_trades(
        nsdl_file, date_str,
        progress_cb=progress_cb,
        auth_cache_path=auth_cache_path,
        upload_fn=upload_nsdl,
        mapin_source_file=str(zero096) if zero096 else nsdl_file,
    )


def dispatch_trades(file_0096: str, date_str: str,
                    progress_cb=None,
                    auth_cache_path: str = '',
                    upload_fn=None,
                    mapin_source_file: str = '') -> DispatchResult:
    """
    Full post-trade-recon automation:
      1. Upload file to WS (0096 by default; NSDL via upload_fn=upload_nsdl)
      2. Allocate block trades (verify no order mismatches, click Allocate)
      3. Download Custody Interface file per involved custodian
      4. Email each file to the custodian

    progress_cb(stage, detail): optional callback.
    auth_cache_path: optional override for the WS session cache JSON path.
    upload_fn: override the upload function — defaults to upload_0096.
               Pass upload_nsdl to dispatch via the NSDL mapper (mapid=195).
    mapin_source_file: optional separate file to read custodian mapin
               routing from. Defaults to file_0096 (the uploaded file).
               NSDL dispatch uses this to drive routing from the 0096
               file while uploading the NSDL file.
    """
    from datetime import datetime
    if upload_fn is None:
        upload_fn = upload_0096
    if not mapin_source_file:
        mapin_source_file = file_0096

    # Step 1: Upload (0096 or NSDL)
    upload_result = upload_fn(file_0096, progress_cb=progress_cb,
                              auth_cache_path=auth_cache_path)
    if not upload_result.ok and upload_result.detail != "duplicate":
        return DispatchResult(False, upload_result, [])

    # Step 2: Allocate block trades
    posting_id = upload_result.posting_id
    alloc_result = None
    if posting_id and posting_id != "0":
        alloc_result = allocate_block_trades(
            posting_id,
            auth_cache_path=auth_cache_path,
            progress_cb=progress_cb,
        )
        if not alloc_result.ok:
            if progress_cb:
                progress_cb("allocate", f"Allocation failed: {alloc_result.message}")
            return DispatchResult(False, upload_result, [],
                                  allocation_result=alloc_result)
    else:
        log.warning("No posting ID from upload — skipping block-trade allocation")

    # Determine date in DD/MM/YYYY for WS forms
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        ws_date = dt.strftime("%d/%m/%Y")
    except ValueError:
        ws_date = date_str

    # Step 3: Determine involved MAPINs grouped by custodian. Use the
    # mapin_source_file override so NSDL dispatch can still route via the
    # 0096 file's MAPIN column (NSDL's CN file doesn't have a MAPIN col).
    by_custodian = _involved_mapins_by_custodian(date_str,
                                                  file_0096=mapin_source_file)
    config = _load_dispatch_config()
    custodian_cfg = config.get("custodians", {})
    common_cfg    = config.get("common", {})

    if not by_custodian:
        msg = ("Could not determine which custodians to notify from today's "
               "trade recon results — aborting dispatch (no emails sent). "
               "Check that a trade recon has been run and that every 0096 "
               "mapin resolves to a pool in pools_hub.json.")
        if progress_cb:
            progress_cb("dispatch", msg)
        return DispatchResult(False, upload_result,
                              [{"custodian": "(none)", "error": msg,
                                "files": [], "email_ok": False}])

    total_mapins = sum(len(v) for v in by_custodian.values())
    if progress_cb:
        progress_cb("dispatch",
                     f"Dispatching {total_mapins} MAPIN(s) across "
                     f"{len(by_custodian)} custodian(s): "
                     f"{', '.join(sorted(by_custodian.keys()))}")

    # Step 4: Login to WS for custody interface downloads
    from ws_downloader import _login as _dl_login, _base_url as _dl_base
    auth_cache = Path(__file__).parent / "config" / "ws_auth.json"
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
    _base = Path(os.environ.get("KEYSTONE_DATA_DIR") or str(Path(__file__).parent))
    out_dir = _base / "data" / date_str / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    custodian_results = []

    # Defense-in-depth: a successful allocate/auth/post is a hard
    # precondition for sending custody emails. The early-return at the
    # top of this function already aborts when alloc fails, but this
    # assertion prevents a future refactor from accidentally routing
    # past it. If alloc_result is None, the upload was a duplicate and
    # no allocation was performed — emails are still safe in that
    # branch because the underlying posting was already processed.
    if alloc_result is not None and not alloc_result.ok:
        msg = (f"Allocation did not succeed — skipping custody dispatch. "
               f"{alloc_result.message}")
        if progress_cb:
            progress_cb("dispatch", msg)
        return DispatchResult(
            False, upload_result,
            [{"custodian": "(none)", "error": msg, "files": [], "email_ok": False}],
            allocation_result=alloc_result,
        )

    # Step 5: Download per MAPIN, email per custodian
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

        # Build subject: list all MAPINs
        mapin_list = ', '.join(m for m, _, _ in downloaded_files)
        subject = subject_tpl.replace("{mapin}", mapin_list).replace("{date}", date_str).replace("{custodian}", cust)
        body    = body_tpl.replace("{mapin}", mapin_list).replace("{date}", date_str).replace("{custodian}", cust)

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
                                  date_str, body_html=body, send_from=send_from)
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
    if alloc_result and not alloc_result.ok:
        all_ok = False
    if progress_cb:
        progress_cb("complete", "Dispatch complete")
    return DispatchResult(all_ok, upload_result, custodian_results,
                          allocation_result=alloc_result)


def _send_custodian_email(recipients: list, subject: str,
                          attachment_paths, custodian: str,
                          date_str: str, body_html: str = '',
                          send_from: str = ''):
    """Send custody interface file(s) to a custodian via M365 Graph API.

    attachment_paths: single Path or list of Paths
    send_from: mailbox to send from (Graph API /users/{send_from}/sendMail).
               Defaults to the configured AZURE_MAILBOX if empty.
    """
    import sys, os
    sys.path.insert(0, str(Path(__file__).parent))

    from core.email_ingestor import EmailIngestor
    import json

    az_path = _cfg_file("azure.json")
    az_file = json.loads(az_path.read_text()) if az_path.exists() else {}
    cfg = {
        "tenant_id":     az_file.get("tenant_id")     or os.environ.get("AZURE_TENANT_ID", ""),
        "client_id":     az_file.get("client_id")     or os.environ.get("AZURE_CLIENT_ID", ""),
        "client_secret": az_file.get("client_secret") or os.environ.get("AZURE_CLIENT_SECRET", ""),
        "mailbox":       az_file.get("mailbox")        or os.environ.get("AZURE_MAILBOX", ""),
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

    # Test-mode rewrite — diverts to a single address when configured.
    recipients, body_html = ingestor._apply_test_mode(recipients, body_html)

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


# ── Client Master Upload (mapcode=CL, mapid=1) ───────────────────────────────
#
# Browser flow observed:
#   1. GET  redirect.do?target=clientDataUpload&mapcode=CL&...     (file picker)
#   2. POST <form-action>  multipart with the XLS                  (upload)
#   3. POST <next-form-action>  with mapid=1                       (mapper run)
#
# Unlike the 0096 upload there is no separate duplicate-check step — the
# mapper runs synchronously and returns the result page in step 3.

CLIENT_UPLOAD_FORM_URL = (
    "redirect.do?target=clientDataUpload&mapcode=CL&scope=*&cmScope=*"
    "&consolidation=C&srcMenuId=1729"
)
# GST Parameter Master upload (mapid=1033, actcode=CM). Same 3-step
# pattern as Account Creation; different mapcode (CM vs CL) and a
# different srcMenuId (4023 vs 1729). The mapper INSERTs into
# CLIENT_GSTPARAM_M when no row exists for the client; otherwise it
# UPDATEs in place. The picker callback for actcode=CM is
#   mapIdPickList('forms[0]','mapid','source','CM')
# and the lone CM row in intmaps_m maps to source='client_mst_map'.
GST_UPLOAD_FORM_URL = (
    "redirect.do?target=dataUpload&mapcode=CM&scope=*&cmScope=*"
    "&consolidation=C&srcMenuId=4023"
)
GST_MAP_ID     = "1033"
GST_MAP_SOURCE = "client_mst_map"
CLIENT_MAP_ID = "1"
# The map-id pick-list (protected/mapIdPickList.jsp?actcode=CL) renders
# exactly one row for CL uploads — clicking it calls:
#   PickList('forms[0]','1','newaccount_map','N','null','CL','N','Y')
# which, in the browser, sets:
#   forms[0].mapid.value  = '1'
#   forms[0].source.value = 'newaccount_map'
#   forms[0].periodFlag.value = 'N'  (because upfrontPeriodFlag='N')
# Our code bypasses the popup by hardcoding these — the mapper won't run
# without a non-empty source value.
CLIENT_MAP_SOURCE = "newaccount_map"


def upload_client_master(file_path: str, progress_cb=None,
                         auth_cache_path: str = '') -> UploadResult:
    """Upload a single-client Account Creation XLS to WealthSpectrum.

    Three-step flow:
      1. GET  upload form    → scrape CSRF + form action
      2. POST file (multipart)  → server stores temp file, returns mapper select page
      3. POST with mapid=1   → run the mapper, return result HTML

    progress_cb(stage, detail): optional callback for UI progress updates.
    """
    fpath = Path(file_path)
    if not fpath.exists():
        return UploadResult(False, f"File not found: {fpath}")

    trace_dir = _trace_dir() if _trace_enabled() else None
    if trace_dir:
        log.info(f"WS client-master upload trace enabled → {trace_dir}")

    base = _base_url()

    # Auth cache — same convention as upload_0096
    if auth_cache_path:
        auth_cache = Path(auth_cache_path)
    else:
        _data_dir = os.environ.get("KEYSTONE_DATA_DIR")
        if _data_dir:
            auth_cache = Path(_data_dir) / "config" / "ws_upload_auth.json"
        else:
            auth_cache = Path(__file__).parent / "config" / "ws_upload_auth.json"
    auth_cache.parent.mkdir(parents=True, exist_ok=True)

    session = _requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"{base}/",
    })

    # ── Login ───────────────────────────────────────────────────────────
    if progress_cb:
        progress_cb("login", "Logging in to WealthSpectrum...")
    try:
        _login_upload(session, auth_cache)
    except Exception as e:
        return UploadResult(False, f"Login failed: {e}")

    _dump_dir = Path(os.environ.get("KEYSTONE_DATA_DIR") or
                     str(Path(__file__).parent)) / "data" / "ws_client_upload_probe"
    _dump_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: GET the upload form ─────────────────────────────────────
    if progress_cb:
        progress_cb("form", "Loading client upload form...")
    form_url = f"{base}/{CLIENT_UPLOAD_FORM_URL}"
    try:
        _req1 = _requests.Request("GET", form_url, headers={"Referer": f"{base}/"})
        _prep1 = session.prepare_request(_req1)
        if trace_dir: _trace_write(trace_dir, "step1", "req", _prep1)
        r1 = session.send(_prep1, timeout=30)
        if trace_dir: _trace_write(trace_dir, "step1", "resp", r1)
        r1.raise_for_status()
    except Exception as e:
        return UploadResult(False, f"Failed to load client upload form: {e}")

    soup1 = BeautifulSoup(r1.text, "html.parser")
    form1 = soup1.find("form")
    if not form1:
        return UploadResult(False, "No form found on client upload page")
    action1 = form1.get("action", "")
    hidden1 = {}
    file_input_name = None
    for inp in soup1.find_all("input"):
        nm = inp.get("name")
        if not nm:
            continue
        itype = (inp.get("type") or "").lower()
        if itype == "file":
            file_input_name = nm
        elif itype in ("hidden", ""):
            hidden1[nm] = inp.get("value", "") or ""
    # submit_check() in the form sets sname = filepath.value, which in
    # modern browsers evaluates to 'C:\\fakepath\\<filename>'. Mimic that
    # exactly — some JSPs validate sname matches the multipart filename
    # field and a bare filename fails that check silently.
    hidden1["sname"] = f"C:\\fakepath\\{fpath.name}"
    # Fallback to the 0096 field name if the client upload form doesn't
    # declare its file input (some JSPs render it via JS).
    file_input_name = file_input_name or "filepath"
    # Include the submit button's name=value. JSPs key off this to
    # distinguish a true submission from an initial GET of the page;
    # without it WS returns tempfile=failure (silent no-op).
    hidden1["submit"] = "Upload"
    log.info(f"Client upload step 1 — action={action1}, "
             f"hidden={list(hidden1.keys())}, file_input={file_input_name!r}")
    (_dump_dir / "step1_form.html").write_text(r1.text, encoding="utf-8", errors="replace")

    # ── Step 2: POST the file ───────────────────────────────────────────
    # POST to the scraped form action — this is a JSP that self-dispatches
    # (the form action and the page URL are the same, and WS returns the
    # next mapper page in the response body). Posting to clientDataPosting.do
    # directly bounces to the dashboard; posting to the JSP returns the
    # real mapper form with a tempfile value.
    if progress_cb:
        progress_cb("upload", f"Uploading {fpath.name}...")
    post_url = f"{base}/{action1}" if not action1.startswith("http") else action1
    log.info(f"Client upload step 2 target: {post_url}")
    _ext = fpath.suffix.lower()
    _mime = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
             if _ext == ".xlsx"
             else "application/vnd.ms-excel")
    try:
        with open(fpath, "rb") as f:
            files = {file_input_name: (fpath.name, f, _mime)}
            req = _requests.Request(
                "POST", post_url,
                data=hidden1, files=files,
                headers={
                    "Referer": form_url,
                    # Browsers include Origin on cross-origin form posts.
                    # WS CSRF guard may reject POSTs without it.
                    "Origin": base.rstrip('/').split('/fincrm')[0],
                },
            )
            prepped = session.prepare_request(req)
            if trace_dir: _trace_write(trace_dir, "step2", "req", prepped)
            r2 = session.send(prepped, timeout=120)
            if trace_dir: _trace_write(trace_dir, "step2", "resp", r2)
        r2.raise_for_status()
        (_dump_dir / "step2_response.html").write_text(
            r2.text, encoding="utf-8", errors="replace")
    except Exception as e:
        return UploadResult(False, f"Client file upload failed: {e}")

    # Parse step 2 response for the mapper-select form
    fields2 = _scrape_all_inputs(r2.text)
    if "tempfile" not in fields2:
        soup2 = BeautifulSoup(r2.text, "html.parser")
        err = soup2.find(string=re.compile(r"error|invalid|fail|denied|not\s+allowed", re.I))
        body_text = re.sub(r"\s+", " ", soup2.get_text()).strip()
        detail_parts = []
        if err:
            detail_parts.append(f"Match: {err.strip()[:200]}")
        detail_parts.append(f"Body: {body_text[:800]}")
        detail_parts.append(f"Fields scraped: {sorted(fields2.keys())}")
        return UploadResult(False, "Upload form did not return expected mapper page",
                            " | ".join(detail_parts))
    _tempfile_val = fields2.get("tempfile", "")
    log.info(f"Client upload step 2 — tempfile={_tempfile_val!r} | "
             f"scraped hidden={sorted(fields2.keys())}")
    # Log every non-hidden input (submit buttons, selects, etc.) — these
    # tell us what the browser would include when the user clicks Submit
    # on the map-id form.
    try:
        _soup2 = BeautifulSoup(r2.text, "html.parser")
        _map_form = _soup2.find("form")
        if _map_form:
            _inputs_summary = []
            for _inp in _map_form.find_all(["input", "select", "button"]):
                _t = (_inp.get("type") or _inp.name or "").lower()
                _n = _inp.get("name") or ""
                _v = _inp.get("value") or ""
                if _t in ("submit", "button", "reset", "checkbox", "radio", "select"):
                    _inputs_summary.append(f"{_t}[name={_n!r},value={_v!r}]")
            log.info(f"Client upload step 2 map-id form buttons/selects: "
                     f"{_inputs_summary}")
    except Exception as _e:
        log.warning(f"Step 2 introspection failed: {_e}")
    if _tempfile_val.lower() == "failure":
        # WS accepted the request but couldn't store the file. Surface an
        # actionable error rather than proceeding through the remaining
        # steps with a broken tempfile reference.
        soup2 = BeautifulSoup(r2.text, "html.parser")
        body_text = re.sub(r"\s+", " ", soup2.get_text()).strip()
        # Also surface request context — useful to compare against browser
        # DevTools when diagnosing why WS silently rejected the file.
        req_ctx = (
            f"file={fpath.name} ({fpath.stat().st_size} bytes, mime={_mime}) | "
            f"sent fields={sorted(hidden1.keys())} | "
            f"scraped fields={sorted(fields2.keys())} | "
            f"status={r2.status_code} len={len(r2.text)}"
        )
        log.warning(f"tempfile=failure  |  {req_ctx}")
        return UploadResult(
            False,
            "WS rejected the file at the upload step (tempfile=failure).",
            f"{req_ctx} | body: {body_text[:800]}",
        )

    # Step 3 mapper endpoint — use the form's scraped action, which WS sets
    # to /fincrm/clientDataPosting.do once the file is stored. That's the
    # mapper controller; POSTing the Submit button's form fields there
    # runs the mapper and returns either the wait page or the result.
    soup2 = BeautifulSoup(r2.text, "html.parser")
    form2 = soup2.find("form")
    scraped_action = form2.get("action", "") if form2 else ""
    if scraped_action:
        if scraped_action.startswith("http"):
            map_post_url = scraped_action
        else:
            map_post_url = f"{base}/{scraped_action.lstrip('/').removeprefix('fincrm/')}"
    else:
        map_post_url = f"{base}/clientDataPosting.do"
    log.info(f"Client upload step 3 target: {map_post_url}")
    map_page_url = f"{base}/protected/clientDataPosting.jsp?mapcode=CL"

    # ── Step 3: Run the mapper with mapid=1 ─────────────────────────────
    if progress_cb:
        progress_cb("posting", "Running client master mapper...")

    # Browser-equivalent body for the map-id form. The map-id picker
    # popup (protected/mapIdPickList.jsp?actcode=CL) calls:
    #   PickList('forms[0]','1','newaccount_map','N','null','CL','N','Y')
    # which assigns THREE fields on the parent form before submit:
    #   mapid      = '1'              (picker also re-enables the disabled input)
    #   source     = 'newaccount_map'
    #   periodFlag = 'N'
    # Missing any of these → WS falls back to "View Client Upload Data"
    # Query page rather than running the mapper. The scraped `source`
    # from step 2 is a placeholder — force the picker-set values.
    _BROWSER_STEP3_KEYS = {
        "format", "file", "tempfile", "WS_CSRFTOKEN",
    }
    out = {k: v for k, v in fields2.items() if k in _BROWSER_STEP3_KEYS}
    out["mapid"]      = CLIENT_MAP_ID          # "1"
    out["source"]     = CLIENT_MAP_SOURCE      # "newaccount_map"
    out["periodFlag"] = "N"
    out["submit"]     = "Submit"
    tf = out.get("tempfile") or fpath.name
    out["tempfile"] = tf
    out["file"]     = tf

    # Log the body we'll send (mask CSRF for brevity)
    _body_dbg = {k: (v if k != "WS_CSRFTOKEN" else "<csrf>") for k, v in out.items()}
    log.info(f"Client upload step 3 body: {_body_dbg}")

    try:
        _req3 = _requests.Request("POST", map_post_url, data=out,
                                  headers={"Referer": map_page_url})
        _prep3 = session.prepare_request(_req3)
        if trace_dir: _trace_write(trace_dir, "step3", "req", _prep3)
        r3 = session.send(_prep3, timeout=180)
        if trace_dir: _trace_write(trace_dir, "step3", "resp", r3)
        r3.raise_for_status()
    except Exception as e:
        return UploadResult(False, f"Client mapper run failed: {e}")
    log.info(f"Client upload step 3 — mapper response ({len(r3.text)} bytes)")
    (_dump_dir / "step3_result.html").write_text(r3.text, encoding="utf-8", errors="replace")

    counts = _parse_posting_result(r3.text)
    log.info(f"Client upload — step 3 counts: {counts}")

    # ── Step 4: replicate onClkLink → genericExeWait.do ─────────────────
    # The browser flow after the map-id Submit lands on withMenu.jsp
    # (Processing Status). The Client Data Posting row's Status icon has
    # onclick="javascript:onClkLink('S','ClientDataPostingProcessLog_<n>')"
    # which (per the page's inline JS) sets two form fields and submits:
    #   POST /fincrm/genericExeWait.do
    #     scopeType=S
    #     scopeAttr=ClientDataPostingProcessLog_<n>
    #     refreshContent=call
    # The response is the Upload Details page with the counts we parse.
    # If step 3 already returned counts (some WS flows chain the redirects
    # through to the details page), we skip this step.
    if not counts:
        log.info("Client upload — step 3 returned no counts; attempting "
                 "Step 4 (onClkLink → genericExeWait.do)")
        if progress_cb:
            progress_cb("posting", "Fetching Client Data Posting result...")

        m = re.search(
            r"onClkLink\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]",
            r3.text, re.IGNORECASE)
        if m:
            scope_type, scope_attr = m.group(1), m.group(2)
            log.info(f"Client upload step 4 — onClkLink scope: "
                     f"type={scope_type!r} attr={scope_attr!r}")
            exe_url = f"{base}/genericExeWait.do"
            body = {
                "scopeType":      scope_type,
                "scopeAttr":      scope_attr,
                "refreshContent": "call",
            }
            try:
                _req4 = _requests.Request("POST", exe_url, data=body,
                                          headers={"Referer": map_post_url})
                _prep4 = session.prepare_request(_req4)
                if trace_dir: _trace_write(trace_dir, "step4", "req", _prep4)
                r4 = session.send(_prep4, timeout=180)
                if trace_dir: _trace_write(trace_dir, "step4", "resp", r4)
                r4.raise_for_status()
                (_dump_dir / "step4_result.html").write_text(
                    r4.text, encoding="utf-8", errors="replace")
                log.info(f"Client upload — step 4 response ({len(r4.text)} bytes)")
                counts = _parse_posting_result(r4.text)
                log.info(f"Client upload — step 4 counts: {counts}")
            except Exception as e:
                log.warning(f"Client upload step 4 failed: {e}")
        else:
            snippet = re.sub(r"<script[^>]*>.*?</script>", "",
                             r3.text, flags=re.DOTALL | re.IGNORECASE)
            snippet = re.sub(r"<[^>]+>", " ", snippet)
            snippet = re.sub(r"\s+", " ", snippet).strip()[:600]
            return UploadResult(
                False,
                "Upload reached the map-id step but the Processing Status "
                "onClkLink trigger wasn't found on the response page.",
                f"WS response snippet: {snippet}",
            )

    if progress_cb:
        progress_cb("done", "Upload complete")

    total      = counts.get("total", 0)
    processed  = counts.get("processed", 0)
    val_err    = counts.get("validation_errors", 0)
    parse_err  = counts.get("parsing_errors", 0)
    error_det  = counts.get("error_details", "")
    posting_id = counts.get("posting_id", "")

    # WS counts the header row as both a "total record" and a "parsing error".
    # Subtract 1 from each for display, mirroring upload_0096.
    HEADER_ROWS = 1
    disp_total  = max(0, total - HEADER_ROWS) if total > 0 else total
    disp_parse  = max(0, parse_err - HEADER_ROWS) if parse_err > 0 else parse_err

    if processed > 0:
        msg = f"{processed} of {disp_total} client record(s) posted"
        if val_err > 0 or disp_parse > 0:
            parts = []
            if val_err > 0:    parts.append(f"{val_err} validation")
            if disp_parse > 0: parts.append(f"{disp_parse} parsing")
            msg += f" ({', '.join(parts)} error{'s' if (val_err+disp_parse)>1 else ''})"
        if posting_id:
            msg += f" — Posting ID: {posting_id}"
        return UploadResult(True, msg, error_det, posting_id=posting_id)

    if val_err > 0 or disp_parse > 0:
        msg = (f"WS rejected the file: 0 processed, "
               f"{val_err} validation errors, {disp_parse} parsing errors")
        return UploadResult(False, msg, error_det)

    msg = f"0 of {disp_total} client record(s) posted (no errors reported)"
    if posting_id:
        msg += f" — Posting ID: {posting_id}"
    return UploadResult(True, msg, "", posting_id=posting_id)


def upload_client_gst(file_path: str, progress_cb=None,
                       auth_cache_path: str = '') -> UploadResult:
    """Upload the per-client GST Parameter XLS via the WS portal mapper.

    Mirrors upload_client_master's 3-step flow with two differences:
      - mapcode=CM, srcMenuId=4023 (vs mapcode=CL, srcMenuId=1729)
      - mapid=1033, source=client_mst_map

    File layout (no header row, Sheet1):
      A = 'GST'                    UTYPE discriminator
      B = numeric WS client id     CLIENTID
      C = feetype code             FLD1 -> client_gstparam_m.FEETYPE
      D = state code               FLD2 -> CLIENTSTATECODE
      E = service location id      FLD3 -> SERVLOCATEID
      F = GST registration no      FLD4 -> GSTREGNO
      G = valid_from (Excel date)  FLD5 -> VALIDFROM
      (H..Q optional: valid_to, ref codes, address)
    """
    fpath = Path(file_path)
    if not fpath.exists():
        return UploadResult(False, f"File not found: {fpath}")

    trace_dir = _trace_dir() if _trace_enabled() else None
    if trace_dir:
        log.info(f"WS GST upload trace enabled → {trace_dir}")

    base = _base_url()

    if auth_cache_path:
        auth_cache = Path(auth_cache_path)
    else:
        _data_dir = os.environ.get("KEYSTONE_DATA_DIR")
        if _data_dir:
            auth_cache = Path(_data_dir) / "config" / "ws_upload_auth.json"
        else:
            auth_cache = Path(__file__).parent / "config" / "ws_upload_auth.json"
    auth_cache.parent.mkdir(parents=True, exist_ok=True)

    session = _requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"{base}/",
    })

    if progress_cb:
        progress_cb("login", "Logging in to WealthSpectrum...")
    try:
        _login_upload(session, auth_cache)
    except Exception as e:
        return UploadResult(False, f"Login failed: {e}")

    _dump_dir = Path(os.environ.get("KEYSTONE_DATA_DIR") or
                     str(Path(__file__).parent)) / "data" / "ws_gst_upload_probe"
    _dump_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: GET the upload form (mapcode=CM) ───────────────────────
    if progress_cb:
        progress_cb("form", "Loading GST upload form...")
    form_url = f"{base}/{GST_UPLOAD_FORM_URL}"
    try:
        _req1 = _requests.Request("GET", form_url, headers={"Referer": f"{base}/"})
        _prep1 = session.prepare_request(_req1)
        if trace_dir: _trace_write(trace_dir, "step1", "req", _prep1)
        r1 = session.send(_prep1, timeout=30)
        if trace_dir: _trace_write(trace_dir, "step1", "resp", r1)
        r1.raise_for_status()
    except Exception as e:
        return UploadResult(False, f"Failed to load GST upload form: {e}")

    soup1 = BeautifulSoup(r1.text, "html.parser")
    form1 = soup1.find("form")
    if not form1:
        return UploadResult(False, "No form found on GST upload page")
    action1 = form1.get("action", "")
    hidden1 = {}
    file_input_name = None
    for inp in soup1.find_all("input"):
        nm = inp.get("name")
        if not nm:
            continue
        itype = (inp.get("type") or "").lower()
        if itype == "file":
            file_input_name = nm
        elif itype in ("hidden", ""):
            hidden1[nm] = inp.get("value", "") or ""
    hidden1["sname"]  = f"C:\\fakepath\\{fpath.name}"
    file_input_name   = file_input_name or "filepath"
    hidden1["submit"] = "Upload"
    log.info(f"GST upload step 1 — action={action1}, "
             f"hidden={list(hidden1.keys())}, file_input={file_input_name!r}")
    (_dump_dir / "step1_form.html").write_text(r1.text, encoding="utf-8", errors="replace")

    # ── Step 2: POST the file (multipart) ──────────────────────────────
    if progress_cb:
        progress_cb("upload", f"Uploading {fpath.name}...")
    post_url = f"{base}/{action1}" if not action1.startswith("http") else action1
    log.info(f"GST upload step 2 target: {post_url}")
    _ext  = fpath.suffix.lower()
    _mime = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
             if _ext == ".xlsx"
             else "application/vnd.ms-excel")
    try:
        with open(fpath, "rb") as f:
            files = {file_input_name: (fpath.name, f, _mime)}
            req = _requests.Request(
                "POST", post_url,
                data=hidden1, files=files,
                headers={
                    "Referer": form_url,
                    "Origin": base.rstrip('/').split('/fincrm')[0],
                },
            )
            prepped = session.prepare_request(req)
            if trace_dir: _trace_write(trace_dir, "step2", "req", prepped)
            r2 = session.send(prepped, timeout=120)
            if trace_dir: _trace_write(trace_dir, "step2", "resp", r2)
        r2.raise_for_status()
        (_dump_dir / "step2_response.html").write_text(
            r2.text, encoding="utf-8", errors="replace")
    except Exception as e:
        return UploadResult(False, f"GST file upload failed: {e}")

    fields2 = _scrape_all_inputs(r2.text)
    if "tempfile" not in fields2:
        soup2 = BeautifulSoup(r2.text, "html.parser")
        err = soup2.find(string=re.compile(r"error|invalid|fail|denied|not\s+allowed", re.I))
        body_text = re.sub(r"\s+", " ", soup2.get_text()).strip()
        detail_parts = []
        if err:
            detail_parts.append(f"Match: {err.strip()[:200]}")
        detail_parts.append(f"Body: {body_text[:800]}")
        detail_parts.append(f"Fields scraped: {sorted(fields2.keys())}")
        return UploadResult(False, "Upload form did not return expected mapper page",
                            " | ".join(detail_parts))
    _tempfile_val = fields2.get("tempfile", "")
    log.info(f"GST upload step 2 — tempfile={_tempfile_val!r} | "
             f"scraped hidden={sorted(fields2.keys())}")
    if _tempfile_val.lower() == "failure":
        soup2 = BeautifulSoup(r2.text, "html.parser")
        body_text = re.sub(r"\s+", " ", soup2.get_text()).strip()
        req_ctx = (
            f"file={fpath.name} ({fpath.stat().st_size} bytes, mime={_mime}) | "
            f"sent fields={sorted(hidden1.keys())} | "
            f"scraped fields={sorted(fields2.keys())} | "
            f"status={r2.status_code} len={len(r2.text)}"
        )
        log.warning(f"tempfile=failure  |  {req_ctx}")
        return UploadResult(
            False,
            "WS rejected the file at the upload step (tempfile=failure).",
            f"{req_ctx} | body: {body_text[:800]}",
        )

    # Step 3 target — use the form's scraped action
    soup2  = BeautifulSoup(r2.text, "html.parser")
    form2  = soup2.find("form")
    scraped_action = form2.get("action", "") if form2 else ""
    if scraped_action:
        if scraped_action.startswith("http"):
            map_post_url = scraped_action
        else:
            map_post_url = f"{base}/{scraped_action.lstrip('/').removeprefix('fincrm/')}"
    else:
        map_post_url = f"{base}/queryDataPosting.do"
    log.info(f"GST upload step 3 target: {map_post_url}")
    map_page_url = f"{base}/protected/withMenu.jsp?self=queryDataPosting.jsp"

    # ── Step 3: Run the mapper with mapid=1033 ─────────────────────────
    if progress_cb:
        progress_cb("posting", "Running GST mapper...")
    _BROWSER_STEP3_KEYS = {"format", "file", "tempfile", "WS_CSRFTOKEN",
                            "srcMenuId", "firmid"}
    out = {k: v for k, v in fields2.items() if k in _BROWSER_STEP3_KEYS}
    out["mapid"]      = GST_MAP_ID          # "1033"
    out["source"]     = GST_MAP_SOURCE      # "client_mst_map"
    out["periodFlag"] = "N"
    out["submit"]     = "Submit"
    tf = out.get("tempfile") or fpath.name
    out["tempfile"] = tf
    out["file"]     = tf

    _body_dbg = {k: (v if k != "WS_CSRFTOKEN" else "<csrf>") for k, v in out.items()}
    log.info(f"GST upload step 3 body: {_body_dbg}")

    try:
        _req3 = _requests.Request("POST", map_post_url, data=out,
                                  headers={"Referer": map_page_url})
        _prep3 = session.prepare_request(_req3)
        if trace_dir: _trace_write(trace_dir, "step3", "req", _prep3)
        r3 = session.send(_prep3, timeout=180)
        if trace_dir: _trace_write(trace_dir, "step3", "resp", r3)
        r3.raise_for_status()
    except Exception as e:
        return UploadResult(False, f"GST mapper run failed: {e}")
    log.info(f"GST upload step 3 — mapper response ({len(r3.text)} bytes)")
    (_dump_dir / "step3_result.html").write_text(r3.text, encoding="utf-8", errors="replace")

    counts = _parse_posting_result(r3.text)
    log.info(f"GST upload — step 3 counts: {counts}")

    # ── Step 4: replicate onClkLink → genericExeWait.do ───────────────
    if not counts:
        log.info("GST upload — step 3 returned no counts; attempting "
                  "Step 4 (onClkLink → genericExeWait.do)")
        if progress_cb:
            progress_cb("posting", "Fetching GST upload result...")
        m = re.search(
            r"onClkLink\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]",
            r3.text, re.IGNORECASE)
        if m:
            scope_type, scope_attr = m.group(1), m.group(2)
            log.info(f"GST upload step 4 — onClkLink scope: "
                     f"type={scope_type!r} attr={scope_attr!r}")
            exe_url = f"{base}/genericExeWait.do"
            body = {
                "scopeType":      scope_type,
                "scopeAttr":      scope_attr,
                "refreshContent": "call",
            }
            try:
                _req4 = _requests.Request("POST", exe_url, data=body,
                                          headers={"Referer": map_post_url})
                _prep4 = session.prepare_request(_req4)
                if trace_dir: _trace_write(trace_dir, "step4", "req", _prep4)
                r4 = session.send(_prep4, timeout=180)
                if trace_dir: _trace_write(trace_dir, "step4", "resp", r4)
                r4.raise_for_status()
                (_dump_dir / "step4_result.html").write_text(
                    r4.text, encoding="utf-8", errors="replace")
                log.info(f"GST upload — step 4 response ({len(r4.text)} bytes)")
                counts = _parse_posting_result(r4.text)
                log.info(f"GST upload — step 4 counts: {counts}")
            except Exception as e:
                log.warning(f"GST upload step 4 failed: {e}")
        else:
            snippet = re.sub(r"<script[^>]*>.*?</script>", "",
                             r3.text, flags=re.DOTALL | re.IGNORECASE)
            snippet = re.sub(r"<[^>]+>", " ", snippet)
            snippet = re.sub(r"\s+", " ", snippet).strip()[:600]
            return UploadResult(
                False,
                "GST upload reached the map-id step but the Processing "
                "Status onClkLink trigger wasn't found on the response page.",
                f"WS response snippet: {snippet}",
            )

    if progress_cb:
        progress_cb("done", "Upload complete")

    total      = counts.get("total", 0)
    processed  = counts.get("processed", 0)
    val_err    = counts.get("validation_errors", 0)
    parse_err  = counts.get("parsing_errors", 0)
    error_det  = counts.get("error_details", "")
    posting_id = counts.get("posting_id", "")

    if processed > 0:
        msg = f"{processed} of {total} GST record(s) posted"
        if val_err > 0 or parse_err > 0:
            parts = []
            if val_err > 0:   parts.append(f"{val_err} validation")
            if parse_err > 0: parts.append(f"{parse_err} parsing")
            msg += f" ({', '.join(parts)} error{'s' if (val_err+parse_err)>1 else ''})"
        if posting_id:
            msg += f" — Posting ID: {posting_id}"
        return UploadResult(True, msg, error_det, posting_id=posting_id)

    if val_err > 0 or parse_err > 0:
        msg = (f"WS rejected the file: 0 processed, "
               f"{val_err} validation errors, {parse_err} parsing errors")
        return UploadResult(False, msg, error_det)

    msg = f"0 of {total} GST record(s) posted (no errors reported)"
    if posting_id:
        msg += f" — Posting ID: {posting_id}"
    return UploadResult(True, msg, "", posting_id=posting_id)


# ── GST authorization helpers ─────────────────────────────────────────── #
# After a successful mapid=1033 upload, the staged rows sit in
# CLIENTTEMPLATE_UPLOAD with STATUS='V' (Verified). To commit them into
# CLIENT_GSTPARAM_M the operator opens
#   clientMasterUpload.do?mode=query
# sets templateType='GST', clicks Query, then authorizes each row from
# the resulting list. The Query step alone is implemented here; the
# authorize step will be layered on top once we see the list HTML.

GST_QUERY_URL = "clientMasterUpload.do?mode=query"

# WS SQL Gateway (sqlgatewayuser.do) — the in-portal SELECT runner. We
# drive it the same way the Database Query screen does in the browser.
SQL_GATEWAY_URL = "sqlgatewayuser.do"


def _sql_gateway_encode(sql: str) -> str:
    """Apply the same pre-submit substitutions the WS form JS does on
    ``sqlStatement`` before the POST. The server reverses these — if
    we skip the encoding the WS request filter rejects the SQL."""
    return (sql.replace('<', '&lt;')
               .replace('>', '&gt;')
               .replace('=', '&equal;')
               .replace("'", '&squote;'))


def ws_run_sql(sql: str, *, auth_cache_path: str = '') -> list[dict]:
    """Run a read-only SQL through the WS portal's Database Query screen.

    Returns a list of row dicts keyed by column header (uppercase, as
    Oracle returns them). Empty list when no rows.

    Mirrors the browser flow:
      1. GET  sqlgatewayuser.do  → scrape CSRF + form
      2. POST sqlgatewayuser.do  (mode=query, reportformat=html,
                                    sqlStatement=<encoded SQL>,
                                    submit=Execute)
      3. Parse the result HTML table
    """
    trace_dir = _trace_dir() if _trace_enabled() else None
    base      = _base_url()

    if auth_cache_path:
        auth_cache = Path(auth_cache_path)
    else:
        _data_dir = os.environ.get("KEYSTONE_DATA_DIR")
        if _data_dir:
            auth_cache = Path(_data_dir) / "config" / "ws_upload_auth.json"
        else:
            auth_cache = Path(__file__).parent / "config" / "ws_upload_auth.json"
    auth_cache.parent.mkdir(parents=True, exist_ok=True)

    session = _requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"{base}/",
    })
    _login_upload(session, auth_cache)

    form_url = f"{base}/{SQL_GATEWAY_URL}"
    r1 = session.get(form_url, headers={"Referer": f"{base}/"}, timeout=30)
    if trace_dir: _trace_write(trace_dir, "sql_get", "resp", r1)
    r1.raise_for_status()

    soup1 = BeautifulSoup(r1.text, "html.parser")
    form  = (soup1.find("form", attrs={"name": "sQLGatewayForm"})
             or soup1.find("form"))
    if not form:
        raise RuntimeError("SQL Gateway form not found")
    body: dict = {}
    for inp in form.find_all("input"):
        nm = inp.get("name")
        if not nm:
            continue
        itype = (inp.get("type") or "").lower()
        if itype in ("submit", "reset", "button"):
            continue
        # Radio buttons only contribute if checked (browser equivalent)
        if itype == "radio" and not inp.has_attr("checked"):
            continue
        body[nm] = inp.get("value", "") or ""
    # CSRF may live outside the form block on this page — scrape it
    # globally to be safe.
    if "WS_CSRFTOKEN" not in body:
        csrf_inp = soup1.find("input", attrs={"name": "WS_CSRFTOKEN"})
        if csrf_inp:
            body["WS_CSRFTOKEN"] = csrf_inp.get("value", "") or ""
    body["mode"]           = "query"
    body["reportformat"]   = "html"
    body["sqlStatement"]   = _sql_gateway_encode(sql)
    body["submit"]         = "Execute"

    action = form.get("action", "") or form_url
    if not action.startswith("http"):
        action = f"{base}/{action.lstrip('/').removeprefix('fincrm/')}"

    _dbg = {k: ('<csrf>' if k == 'WS_CSRFTOKEN'
                else (v[:80] + '...' if k == 'sqlStatement' and len(v) > 80 else v))
            for k, v in body.items()}
    log.info(f"WS SQL Gateway POST {action} body={_dbg}")

    r2 = session.post(action, data=body,
                       headers={"Referer": form_url}, timeout=60)
    if trace_dir: _trace_write(trace_dir, "sql_post", "resp", r2)
    r2.raise_for_status()

    # Save a probe dump for debugging
    _dump_dir = Path(os.environ.get("KEYSTONE_DATA_DIR") or
                     str(Path(__file__).parent)) / "data" / "ws_sql_probe"
    _dump_dir.mkdir(parents=True, exist_ok=True)
    (_dump_dir / "last_result.html").write_text(r2.text, encoding="utf-8", errors="replace")

    return _parse_sql_gateway_result(r2.text)


def ws_lookup_client_id_by_pan(pan: str, *,
                                  auth_cache_path: str = '') -> Optional[int]:
    """Look up the WS-assigned CLIENTID for a given PAN.

    Returns None when no match. The PAN column on CLIENT_M is named
    H1PANNO (first-holder PAN). PAN comparison is case-insensitive on
    the server.
    """
    pan = (pan or '').strip().upper()
    if not pan:
        return None
    sql = (
        "SELECT clientid FROM client_m "
        f"WHERE UPPER(h1panno) = '{pan}' AND ROWNUM = 1"
    )
    rows = ws_run_sql(sql, auth_cache_path=auth_cache_path)
    if not rows:
        return None
    val = rows[0].get('CLIENTID') or rows[0].get('clientid')
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _parse_sql_gateway_result(html: str) -> list[dict]:
    """Parse the WS SQL Gateway result HTML.

    WS renders results as a single nested table located after the
    "Output" label, with column headers marked by ``<td class="heading">``
    and data rows as plain ``<td>``. The TR nesting is broken (WS emits
    open <tr> tags without closers) so BeautifulSoup reparses into a
    flat sequence we can iterate.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Anchor: find the "Output" label, then the next table.
    output_label = soup.find(string=re.compile(r'^\s*Output\s*$', re.I))
    table = None
    if output_label:
        node = output_label.parent
        while node is not None and table is None:
            table = node.find_next("table")
            if table is not None and table.find("td", class_="heading"):
                break
            node = node.parent
    if table is None:
        # Fallback: find ANY table with class="heading" cells
        for t in soup.find_all("table"):
            if t.find("td", class_="heading"):
                table = t
                break
    if table is None:
        return []

    # Walk every <td> inside the table in document order.
    # Headers are <td class="heading"> at the start; everything after
    # (still plain <td>) are data cells in row-major order.
    headers: list[str] = []
    data_cells: list[str] = []
    seen_header_block = False
    for td in table.find_all("td"):
        text = td.get_text(strip=True)
        is_heading = "heading" in (td.get("class") or [])
        if is_heading:
            # Still collecting headers
            if not seen_header_block or not data_cells:
                headers.append(text)
                seen_header_block = True
            else:
                # A heading td after data — shouldn't happen but skip
                continue
        else:
            # Skip non-heading TDs that wrap nested tables (we'd otherwise
            # collect the outer wrapper's empty cell)
            if td.find("table"):
                continue
            if seen_header_block:
                data_cells.append(text)

    if not headers:
        return []
    ncols = len(headers)
    rows: list[dict] = []
    for i in range(0, len(data_cells), ncols):
        chunk = data_cells[i:i + ncols]
        if len(chunk) != ncols:
            break
        rows.append(dict(zip(headers, chunk)))
    return rows


def gst_query_pending(template_type: str = 'GST',
                       as_on_date: str = '',
                       status: str = '',
                       scope: str = '*',
                       auth_cache_path: str = '') -> UploadResult:
    """POST the clientMasterUpload Query form to list staged rows.

    Mirrors the browser flow: GET the form to scrape CSRF + hidden
    fields, then POST with templateType set. The raw HTML response is
    dumped to data/ws_gst_upload_probe/query_result.html for inspection;
    UploadResult.detail carries a short snippet of the table body.

    Args:
        template_type: WS template-type code. Default 'GST'.
        as_on_date: DD/MM/YYYY. Defaults to today (IST).
        status: '' (All), 'New', 'E' (Error), 'V' (Verified),
                'P' (Authorized).
        scope: '*' (Corporate), 'C' (Account), 'P' (Pool), etc.
    """
    if not as_on_date:
        from core.timeutils import ist_now
        as_on_date = ist_now().strftime('%d/%m/%Y')

    trace_dir = _trace_dir() if _trace_enabled() else None
    base      = _base_url()

    if auth_cache_path:
        auth_cache = Path(auth_cache_path)
    else:
        _data_dir = os.environ.get("KEYSTONE_DATA_DIR")
        if _data_dir:
            auth_cache = Path(_data_dir) / "config" / "ws_upload_auth.json"
        else:
            auth_cache = Path(__file__).parent / "config" / "ws_upload_auth.json"
    auth_cache.parent.mkdir(parents=True, exist_ok=True)

    session = _requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"{base}/",
    })

    try:
        _login_upload(session, auth_cache)
    except Exception as e:
        return UploadResult(False, f"Login failed: {e}")

    _dump_dir = Path(os.environ.get("KEYSTONE_DATA_DIR") or
                     str(Path(__file__).parent)) / "data" / "ws_gst_upload_probe"
    _dump_dir.mkdir(parents=True, exist_ok=True)

    # ── GET the query form ─────────────────────────────────────────────
    form_url = f"{base}/{GST_QUERY_URL}"
    try:
        r1 = session.get(form_url, headers={"Referer": f"{base}/"}, timeout=30)
        if trace_dir: _trace_write(trace_dir, "query_get", "resp", r1)
        r1.raise_for_status()
    except Exception as e:
        return UploadResult(False, f"Failed to load Query form: {e}")
    (_dump_dir / "query_form.html").write_text(r1.text, encoding="utf-8", errors="replace")

    # Scrape every hidden + named input so we round-trip the form
    # exactly the way the browser would, including any defaults the
    # JSP set that we shouldn't override.
    soup = BeautifulSoup(r1.text, "html.parser")
    form = soup.find("form", attrs={"name": "clientMasterUploadForm"}) or soup.find("form")
    if not form:
        return UploadResult(False, "No query form on the Authorize page")
    body: dict = {}
    for inp in form.find_all("input"):
        nm = inp.get("name")
        if not nm:
            continue
        itype = (inp.get("type") or "").lower()
        if itype in ("submit", "reset", "button"):
            continue
        body[nm] = inp.get("value", "") or ""
    for sel in form.find_all("select"):
        nm = sel.get("name")
        if not nm:
            continue
        chosen = sel.find("option", selected=True)
        if chosen is not None:
            body[nm] = chosen.get("value", "") or ""
        else:
            first = sel.find("option")
            body[nm] = (first.get("value", "") if first else "") or ""

    # Apply our overrides (browser equivalent of changing the dropdown
    # then clicking Query). NOTE: validate('list') in the browser flips
    # mode='query' to 'list' before submit; sending mode=query just
    # re-renders the empty form.
    body["mode"]         = "list"
    body["templateType"] = template_type
    body["status"]       = status
    body["scope"]        = scope
    body["fromDateStr"]  = as_on_date
    body["toDateStr"]    = as_on_date
    body["query"]        = "Query"

    form_action = form.get("action", "") or f"{base}/clientMasterUpload.do"
    if not form_action.startswith("http"):
        form_action = f"{base}/{form_action.lstrip('/').removeprefix('fincrm/')}"
    _body_dbg = {k: (v if k != "WS_CSRFTOKEN" else "<csrf>") for k, v in body.items()}
    log.info(f"GST query — POST {form_action} body={_body_dbg}")

    # ── POST the query ─────────────────────────────────────────────────
    try:
        r2 = session.post(form_action, data=body,
                           headers={"Referer": form_url}, timeout=60)
        if trace_dir: _trace_write(trace_dir, "query_post", "resp", r2)
        r2.raise_for_status()
    except Exception as e:
        return UploadResult(False, f"Query POST failed: {e}")
    (_dump_dir / "query_result.html").write_text(r2.text, encoding="utf-8", errors="replace")

    # Try to count result rows + extract a body snippet for the caller.
    rs = BeautifulSoup(r2.text, "html.parser")
    rows: list = []
    for table in rs.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            # Heuristic: data rows on this page tend to be wide
            # (>= 8 cells); the page chrome rows are short.
            if len(cells) >= 8:
                rows.append(cells)
    body_text = re.sub(r"\s+", " ", rs.get_text()).strip()
    snippet = body_text[:1200]

    msg = f"Query returned {len(rows)} candidate row(s) for templateType={template_type!r}"
    detail = (f"as_on={as_on_date} status={status!r} scope={scope!r} | "
              f"HTML dumped to data/ws_gst_upload_probe/query_result.html | "
              f"first row: {rows[0] if rows else '(none)'} | "
              f"body: {snippet[:600]}")
    return UploadResult(True, msg, detail)


def gst_authorize_pending(template_type: str = 'GST',
                           as_on_date: str = '',
                           scope: str = '*',
                           auth_cache_path: str = '') -> UploadResult:
    """Authorise all pending GST staging rows for the as-on date.

    Browser flow being replicated:
      1. GET  clientMasterUpload.do?mode=query
      2. POST clientMasterUpload.do  (mode=query, templateType=GST,
                                       status='' (All), query=Query)
      3. On the list page, set every row's .authorize='on'
      4. POST clientMasterUpload.do  (mode=list, authorise='Authorise',
                                       ...row fields...)

    WS handles Verify + Authorise server-side when Authorise is clicked
    on rows that haven't been verified yet, so a single pass with the
    status='' (All) filter is sufficient.
    """
    if not as_on_date:
        from core.timeutils import ist_now
        as_on_date = ist_now().strftime('%d/%m/%Y')

    trace_dir = _trace_dir() if _trace_enabled() else None
    base      = _base_url()

    if auth_cache_path:
        auth_cache = Path(auth_cache_path)
    else:
        _data_dir = os.environ.get("KEYSTONE_DATA_DIR")
        if _data_dir:
            auth_cache = Path(_data_dir) / "config" / "ws_upload_auth.json"
        else:
            auth_cache = Path(__file__).parent / "config" / "ws_upload_auth.json"
    auth_cache.parent.mkdir(parents=True, exist_ok=True)

    session = _requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"{base}/",
    })

    try:
        _login_upload(session, auth_cache)
    except Exception as e:
        return UploadResult(False, f"Login failed: {e}")

    _dump_dir = Path(os.environ.get("KEYSTONE_DATA_DIR") or
                     str(Path(__file__).parent)) / "data" / "ws_gst_upload_probe"
    _dump_dir.mkdir(parents=True, exist_ok=True)

    def _scrape_form_fields(html: str) -> tuple[dict, str]:
        """Round-trip every named input/select inside the
        clientMasterUploadForm. Returns (body, action_url)."""
        s = BeautifulSoup(html, "html.parser")
        f = s.find("form", attrs={"name": "clientMasterUploadForm"}) or s.find("form")
        if not f:
            return {}, ""
        out: dict = {}
        for inp in f.find_all("input"):
            nm = inp.get("name")
            if not nm:
                continue
            itype = (inp.get("type") or "").lower()
            if itype in ("submit", "reset", "button"):
                continue
            if itype == "checkbox" and not inp.has_attr("checked"):
                continue
            out[nm] = inp.get("value", "") or ""
        for sel in f.find_all("select"):
            nm = sel.get("name")
            if not nm:
                continue
            chosen = sel.find("option", selected=True)
            if chosen is not None:
                out[nm] = chosen.get("value", "") or ""
            else:
                first = sel.find("option")
                out[nm] = (first.get("value", "") if first else "") or ""
        action = f.get("action", "") or f"{base}/clientMasterUpload.do"
        if not action.startswith("http"):
            action = f"{base}/{action.lstrip('/').removeprefix('fincrm/')}"
        return out, action

    # ── Step 1: GET the empty query form ───────────────────────────────
    form_url = f"{base}/{GST_QUERY_URL}"
    try:
        r1 = session.get(form_url, headers={"Referer": f"{base}/"}, timeout=30)
        if trace_dir: _trace_write(trace_dir, "auth_q_get", "resp", r1)
        r1.raise_for_status()
    except Exception as e:
        return UploadResult(False, f"Failed to load Query form: {e}")
    (_dump_dir / "auth_step1_form.html").write_text(r1.text, encoding="utf-8", errors="replace")

    # ── Step 2: POST Query with status='' (All) → get the list page ────
    body_q, action_url = _scrape_form_fields(r1.text)
    if not body_q:
        return UploadResult(False, "Query form not found on Authorize page")
    body_q.update({
        # validate('list') in the browser flips mode='query' to 'list'
        # before submit; WS reads mode=list as "run the query and
        # render the results table". Sending mode=query just re-renders
        # the empty form.
        "mode":         "list",
        "templateType": template_type,
        "status":       "",            # All — WS verifies then authorizes
        "scope":        scope,
        "fromDateStr":  as_on_date,
        "toDateStr":    as_on_date,
        "query":        "Query",
    })
    _dbg = {k: (v if k != "WS_CSRFTOKEN" else "<csrf>") for k, v in body_q.items()}
    log.info(f"GST authorize step 2 — POST Query body={_dbg}")
    try:
        r2 = session.post(action_url, data=body_q,
                           headers={"Referer": form_url}, timeout=60)
        if trace_dir: _trace_write(trace_dir, "auth_q_post", "resp", r2)
        r2.raise_for_status()
    except Exception as e:
        return UploadResult(False, f"Query POST failed: {e}")
    (_dump_dir / "auth_step2_list.html").write_text(r2.text, encoding="utf-8", errors="replace")

    list_html = r2.text
    indices = sorted({
        int(i) for i in re.findall(
            r'name="listDataIndex\[(\d+)\]\.authorize"', list_html
        )
    })
    log.info(f"GST authorize — found {len(indices)} authorizable row(s) at indices {indices[:10]}")
    if not indices:
        rs = BeautifulSoup(list_html, "html.parser")
        body_text = re.sub(r"\s+", " ", rs.get_text()).strip()[:400]
        return UploadResult(
            True,
            f"No staging rows to authorise for templateType={template_type!r}",
            f"as_on={as_on_date} | snippet: {body_text}"
        )

    # ── Step 3: flip authorize=on for each row, POST authorise=Authorise
    body_a, action_url2 = _scrape_form_fields(list_html)
    if not body_a:
        return UploadResult(False, "List form not found in Query response")

    # finalcheck('Authorize') in the browser flips mode='list' to
    # 'Authorize' before submitting. WS reads mode=Authorize as
    # "promote each row whose .authorize=on from staging to live".
    body_a["mode"]           = "Authorize"
    body_a["templateType"]   = template_type
    body_a["status"]         = ""
    body_a["refreshContent"] = "call"
    for i in indices:
        body_a[f"listDataIndex[{i}].authorize"] = "on"
        body_a.setdefault(f"listDataIndex[{i}].delete", "off")
    body_a["authorise"] = "Authorise"

    _dbg2 = {k: (v if k != "WS_CSRFTOKEN" else "<csrf>") for k, v in body_a.items()}
    log.info(f"GST authorize step 3 — POST Authorise body={_dbg2}")

    try:
        r3 = session.post(action_url2, data=body_a,
                           headers={"Referer": form_url}, timeout=120)
        if trace_dir: _trace_write(trace_dir, "auth_post", "resp", r3)
        r3.raise_for_status()
    except Exception as e:
        return UploadResult(False, f"Authorise POST failed: {e}")
    (_dump_dir / "auth_step3_result.html").write_text(r3.text, encoding="utf-8", errors="replace")

    rs = BeautifulSoup(r3.text, "html.parser")
    err_match = rs.find(string=re.compile(r"error|invalid|fail", re.I))
    body_text = re.sub(r"\s+", " ", rs.get_text()).strip()
    snippet   = body_text[:1200]

    if err_match:
        return UploadResult(
            False,
            f"Authorise POST returned an error indication ({len(indices)} row(s) attempted)",
            f"Match: {err_match.strip()[:200]} | snippet: {snippet[:600]}"
        )

    msg = f"Authorise submitted for {len(indices)} row(s) (templateType={template_type!r})"
    detail = (f"as_on={as_on_date} | indices={indices} | "
              f"HTML dumped to data/ws_gst_upload_probe/auth_step3_result.html | "
              f"snippet: {snippet[:600]}")
    return UploadResult(True, msg, detail)


# ============================================================================ #
# ── Pool Creation WS bridge ────────────────────────────────────────────────  #
# 3-step UI flow replicated headlessly:
#   1. generalOption.do (Investment Approach master)  — append row, POST update
#   2. editSchemeMaster.do (Scheme Master)            — POST mode=insert
#   3. editAccountScheme.do (Pool Master)             — POST mode=save
#
# See reference_ws_portal_ajax_patterns.md for the broader WS conventions
# (CSRF rotation, PickList popups that assign multiple parent-form fields,
# withMenu.jsp wrapper pages).
# ============================================================================ #

IA_LIST_URL     = "generalOption.do?mode=modify&moduleName=SCHEMEMASTER&fieldName=INVESTMENTAPPROACH"
# Form action renders as bare generalOption.do; mode is carried in the body
# (step A=addmore, step B=update). Do NOT bake mode into the URL — it
# collides with the body value and WS routes to the URL's copy, breaking
# the addmore step entirely. moduleName/fieldName are sent in the body.
IA_POST_URL     = "generalOption.do"
SCHEME_FORM_URL = "editSchemeMaster.do?mode=create"
SCHEME_POST_URL = "editSchemeMaster.do"
POOL_FORM_URL   = "editAccountScheme.do?mode=create"
POOL_POST_URL   = "editAccountScheme.do"
DP_PICKLIST_URL = "protected/dpPickList.jsp?scope=A&queryField=&form=forms[0]&dp=dp&dpid=dpid"


def _dedupe_session_cookies(session, name: str = "JSESSIONID") -> int:
    """Drop the pre-auth path=/ JSESSIONID ONLY when the authenticated
    path=/fincrm one also exists.

    Background: WS's auth flow sometimes sets JSESSIONID twice during
    login — first at path=/ from the initial bare GET to '/', then at
    path=/fincrm from the POST to /fincrm/j_spring_security_check.
    Browsers (per RFC 6265) send only the most-specific path match,
    but requests' cookielib sends BOTH. WS reads the ambiguous Cookie
    header as session identity mismatch and 302s the POST to /auth.do.

    BUT — when only path=/ exists (e.g. because the cached session in
    ws_upload_auth.json was saved at path=/), that IS the authenticated
    session. Don't drop it then or we send a no-cookie request, WS
    spawns a fresh session, and the CSRF we hold (bound to the old
    session) gets rejected with errorCSRF.jsp.

    So the rule is: drop path=/ only if path=/fincrm is also present.
    """
    snapshot = list(session.cookies)
    has_fincrm = any(
        c.name == name and c.path.startswith('/fincrm')
        for c in snapshot
    )
    if not has_fincrm:
        return 0  # path=/ (or whatever) is the only session — keep it
    drop_keys = {
        (c.name, c.domain, c.path)
        for c in snapshot
        if c.name == name and c.path == '/'
    }
    if not drop_keys:
        return 0
    session.cookies.clear()
    for c in snapshot:
        if (c.name, c.domain, c.path) in drop_keys:
            continue
        session.cookies.set_cookie(c)
    return len(drop_keys)


def _pc_open_session(auth_cache_path: str = '', force_fresh: bool = True):
    """Open + login a requests.Session for Pool Creation calls.

    force_fresh defaults to True: ALWAYS delete the cached session
    on disk before login, forcing a real j_spring_security_check
    round-trip. Required because WS gates write operations (IA
    addmore/update, scheme insert, bank create, pool save) to
    *recently-authenticated* sessions, not merely *not-expired*
    sessions. A cached session passes the read-validity GET check
    but POSTs from it bounce to /auth.do with the response setting
    a brand-new JSESSIONID. Read-validity ≠ write-validity on this
    WS install.

    The previous default (force_fresh=False) was insufficient because
    callers without an updated argument list would never opt in.
    Defaulting to True ensures every caller gets a write-eligible
    session, and the ~3s fresh-login overhead is well within budget
    for pool-creation-frequency operations. Callers that need cache
    reuse can pass force_fresh=False explicitly.
    """
    base = _base_url()
    if auth_cache_path:
        auth_cache = Path(auth_cache_path)
    else:
        _data_dir = os.environ.get("KEYSTONE_DATA_DIR")
        if _data_dir:
            auth_cache = Path(_data_dir) / "config" / "ws_upload_auth.json"
        else:
            auth_cache = Path(__file__).parent / "config" / "ws_upload_auth.json"
    auth_cache.parent.mkdir(parents=True, exist_ok=True)

    if force_fresh and auth_cache.exists():
        try:
            auth_cache.unlink()
            log.info(f"_pc_open_session: deleted cached session at {auth_cache} "
                     f"(force_fresh=True)")
        except Exception as e:
            log.warning(f"_pc_open_session: could not delete cache: {e}")

    session = _requests.Session()
    # User-Agent MUST match what the per-POST overlay uses — WS binds
    # session validity to UA fingerprint. If login + listing GET use
    # one UA and the addmore POST overlays a different UA, WS bounces
    # the POST to /auth.do with a fresh JSESSIONID. Local probe Phase 2
    # works because it sets the same Chrome/148 UA throughout; this
    # session helper used to set Chrome/120 here, then ADDMORE_HEADERS
    # overlaid Chrome/148 — that mid-session UA change was the
    # production-only failure mode that survived even fresh login.
    session.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"),
        "Referer": f"{base}/",
    })
    _login_upload(session, auth_cache)
    return session, base


def _pc_open_apply_session(auth_cache_path: str = ''):
    """Open + login a requests.Session as the Apply Corp Actions user
    (FINCRM_USER2 / FINCRM_PASS2). Distinct from _pc_open_session
    because the WS portal forbids the same user from doing both the
    upload and the apply.
    """
    from ws_downloader import _login

    if not apply_creds_configured():
        raise RuntimeError(
            "Apply credentials not configured. Set FINCRM_USER2 / FINCRM_PASS2."
        )

    base = _base_url()
    if auth_cache_path:
        auth_cache = Path(auth_cache_path)
    else:
        _data_dir = os.environ.get("KEYSTONE_DATA_DIR")
        if _data_dir:
            auth_cache = Path(_data_dir) / "config" / "ws_apply_auth.json"
        else:
            auth_cache = Path(__file__).parent / "config" / "ws_apply_auth.json"
    auth_cache.parent.mkdir(parents=True, exist_ok=True)

    session = _requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"{base}/",
    })
    # Swap env vars during login so ws_downloader._login picks up the
    # apply user, then restore — keeps the upload/download flow on the
    # default FINCRM_USER untouched.
    orig_user = os.environ.get("FINCRM_USER", "")
    orig_pass = os.environ.get("FINCRM_PASS", "")
    had_user  = "FINCRM_USER" in os.environ
    had_pass  = "FINCRM_PASS" in os.environ
    try:
        os.environ["FINCRM_USER"] = _apply_user()
        os.environ["FINCRM_PASS"] = _apply_pass()
        _login(session, auth_cache)
    finally:
        if had_user:
            os.environ["FINCRM_USER"] = orig_user
        else:
            os.environ.pop("FINCRM_USER", None)
        if had_pass:
            os.environ["FINCRM_PASS"] = orig_pass
        else:
            os.environ.pop("FINCRM_PASS", None)

    return session, base


def ws_list_investment_approaches(session=None, base=None,
                                  auth_cache_path: str = '') -> dict:
    """GET generalOption.do and parse every categoryListBean[i] row.

    Returns: {rows, csrf, total_rows, session, base}.
    Callers that will POST back a modified list should reuse the returned
    session + csrf to stay within the same server-side transaction.
    """
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)
    # allow_redirects=False so an auth bounce (302 to /auth.do or
    # loginScreen.do) is visible — otherwise requests silently follows
    # the redirect and we'd parse the login page as if it were the
    # form. Local probe uses this same flag.
    r = session.get(f"{base}/{IA_LIST_URL}", timeout=30,
                    allow_redirects=False)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    rows = []
    i = 0
    while True:
        val_inp = soup.find("input", {"name": f"categoryListBean[{i}].optionValue"})
        if not val_inp:
            break
        desc_inp = soup.find("input", {"name": f"categoryListBean[{i}].optionDesc"})
        seq_inp  = soup.find("input", {"name": f"categoryListBean[{i}].optionSeq"})
        def_inp  = soup.find("input", {"name": f"categoryListBean[{i}].defaultFlag"})
        rows.append({
            "option_value": (val_inp.get("value") or "").strip(),
            "option_desc":  (desc_inp.get("value") if desc_inp else "") or "",
            "option_seq":   (seq_inp.get("value") if seq_inp else "") or "",
            "default":      bool(def_inp and def_inp.has_attr("checked")),
        })
        i += 1

    csrf_inp = soup.find("input", {"name": "WS_CSRFTOKEN"})
    tot_inp  = soup.find("input", {"name": "totalRows"})
    return {
        "rows":       rows,
        "csrf":       (csrf_inp.get("value") if csrf_inp else "") or "",
        "total_rows": int((tot_inp.get("value") if tot_inp else "0") or "0"),
        "session":    session,
        "base":       base,
    }


def _parse_ia_rows_from_html(html: str) -> tuple:
    """Return (rows, csrf, total_rows) parsed from an IA-list response page."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    i = 0
    while True:
        val_inp = soup.find("input", {"name": f"categoryListBean[{i}].optionValue"})
        if not val_inp:
            break
        desc_inp = soup.find("input", {"name": f"categoryListBean[{i}].optionDesc"})
        seq_inp  = soup.find("input", {"name": f"categoryListBean[{i}].optionSeq"})
        def_inp  = soup.find("input", {"name": f"categoryListBean[{i}].defaultFlag"})
        rows.append({
            "option_value": (val_inp.get("value") or "").strip(),
            "option_desc":  (desc_inp.get("value") if desc_inp else "") or "",
            "option_seq":   (seq_inp.get("value") if seq_inp else "") or "",
            "default":      bool(def_inp and def_inp.has_attr("checked")),
        })
        i += 1
    csrf_inp = soup.find("input", {"name": "WS_CSRFTOKEN"})
    tot_inp  = soup.find("input", {"name": "totalRows"})
    return (rows,
            (csrf_inp.get("value") if csrf_inp else "") or "",
            int((tot_inp.get("value") if tot_inp else "0") or "0"))


def _ia_row_body(rows: list) -> dict:
    """Serialize a list of IA rows into categoryListBean[i].* form fields."""
    body = {}
    for idx, row in enumerate(rows):
        body[f"categoryListBean[{idx}].optionValue"] = row.get("option_value", "")
        body[f"categoryListBean[{idx}].optionDesc"]  = row.get("option_desc", "")
        body[f"categoryListBean[{idx}].optionSeq"]   = row.get("option_seq", "")
        if row.get("default"):
            body[f"categoryListBean[{idx}].defaultFlag"] = "on"
    return body


def ws_create_investment_approach(ia_code: str, ia_name: str,
                                  ia_no: int = None,
                                  session=None, base=None,
                                  auth_cache_path: str = '') -> dict:
    """Append a new Investment Approach via the browser's two-step flow.

    Browser UX:
      1. Click "Add More" → POST mode=addmore → WS redraws page with an
         extra empty row appended, returns fresh CSRF + totalRows=N+1.
      2. Operator fills the new row.
      3. Click "Save" → POST mode=update with all N+1 rows.

    Replicating only step 3 with an invented extra row doesn't register
    the new slot server-side — WS returns "Action successfully completed"
    but silently discards the new row. Both steps are required.

    If ia_code already exists, returns {'status':'exists','row':…}
    without posting. ia_no is optional — uses max(existing option_seq)+1.
    """
    # Use a custom listing call that mirrors the working probe's flow
    # exactly — allow_redirects=False on the GET so any WS auth bounce
    # is visible (rather than silently followed and parsed as if it
    # were a real form), and explicit headers matching the probe.
    listing = ws_list_investment_approaches(session=session, base=base,
                                             auth_cache_path=auth_cache_path)
    session, base = listing["session"], listing["base"]
    rows = listing["rows"]

    # Dedupe duplicate JSESSIONID cookies before any POST. Multiple
    # JSESSIONIDs in the Cookie header → WS reads it as a session
    # identity mismatch → 302 to /auth.do. Always log the cookie
    # state pre + post so we can verify dedup took effect.
    _pre = [f"{c.name}@{c.domain}{c.path}" for c in session.cookies]
    _removed = _dedupe_session_cookies(session)
    _post = [f"{c.name}@{c.domain}{c.path}" for c in session.cookies]
    log.info(f"WS IA create — cookie dedupe: dropped={_removed} "
             f"pre={_pre} post={_post}")

    code_target = ia_code.strip().upper()
    for r_existing in rows:
        if r_existing["option_value"].strip().upper() == code_target:
            log.info(f"WS IA exists — skipping create for {ia_code}")
            return {"status": "exists", "row": r_existing,
                    "session": session, "base": base}

    if ia_no is None:
        seqs = [int(r["option_seq"] or "0") for r in rows]
        ia_no = (max(seqs) if seqs else 0) + 1

    post_url = f"{base}/{IA_POST_URL}"

    # Origin is scheme + host only (no path). ``base`` is FINCRM_URL
    # which includes '/fincrm' — strip via urlparse so Origin matches
    # the browser's exactly.
    from urllib.parse import urlparse as _urlparse
    _bp = _urlparse(base)
    origin = f"{_bp.scheme}://{_bp.netloc}"

    # Per the operator's UI cURL capture, the browser's Referer header
    # DIFFERS between the two steps — Step A (addmore) carries the
    # listing-mode querystring (the URL the form was rendered on);
    # Step B (update) is bare (the URL the form's action attribute
    # points to). Match exactly — WS validates Referer as part of its
    # CSRF guard and a 'wrong' value gets the 'Requested resource
    # unauthorized' page back.
    addmore_referer = f"{base}/{IA_LIST_URL}"   # with ?mode=modify…
    update_referer  = f"{base}/{IA_POST_URL}"   # bare

    # Mirror every header the browser sends with this POST — WS's CSRF
    # guard cross-checks Origin, Referer, AND Sec-Fetch-* headers (a
    # Chromium-style 'fetch metadata' check). Sending only Origin +
    # Referer leaves Sec-Fetch-Site at requests' default empty value,
    # which WS's check treats as 'cross-site'. Replicating the cURL
    # exactly removes any guesswork — confirmed against the operator's
    # UI capture.
    # Verbatim from the operator's working browser cURL (Edge 148 on
    # Win10). Every header that wasn't already there was added — WS's
    # CSRF / fetch-metadata guard cross-checks the full chrome and
    # rejects requests that don't look like a real browser POST.
    # Header order + content matched to the working probe's
    # ADDMORE_HEADERS exactly. Connection=keep-alive is explicit (the
    # browser sends it; not setting leaves it to requests default
    # which may differ); the order matches Edge's natural alphabetical
    # ordering for the working capture.
    _shared_headers = {
        "Accept":                   ("text/html,application/xhtml+xml,"
                                     "application/xml;q=0.9,image/avif,"
                                     "image/webp,image/apng,*/*;q=0.8,"
                                     "application/signed-exchange;v=b3;q=0.7"),
        "Accept-Language":          "en-US,en;q=0.9,en-IN;q=0.8",
        "Cache-Control":            "no-cache",
        "Connection":               "keep-alive",
        "Content-Type":             "application/x-www-form-urlencoded",
        "DNT":                      "1",
        "Origin":                   origin,
        "Pragma":                   "no-cache",
        "Sec-Fetch-Dest":           "document",
        "Sec-Fetch-Mode":           "navigate",
        "Sec-Fetch-Site":           "same-origin",
        "Sec-Fetch-User":           "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"),
        "sec-ch-ua":                ('"Chromium";v="148", '
                                     '"Microsoft Edge";v="148", '
                                     '"Not/A)Brand";v="99"'),
        "sec-ch-ua-mobile":         "?0",
        "sec-ch-ua-platform":       '"Windows"',
    }
    addmore_headers = {**_shared_headers, "Referer": addmore_referer}
    update_headers  = {**_shared_headers, "Referer": update_referer}

    # ── Step A: POST mode=addmore — WS appends an empty row server-side.
    # Body shape verbatim from operator's UI cURL capture:
    #   mode, menuDisp, totalRows, categoryListBean[0..N-1].*, WS_CSRFTOKEN
    # Note WS_CSRFTOKEN is at the END (after the row fields). Server-
    # side Struts can be sensitive to ordered key parsing — putting CSRF
    # before row fields would split the categoryListBean[*] sequence
    # and may cause the server to read 0 rows. No btnSave / moduleName /
    # fieldName in the body — the browser's capture omits them too.
    addmore_body: dict = {
        "mode":         "addmore",
        "menuDisp":     "null",
        "totalRows":    str(len(rows)),
    }
    addmore_body.update(_ia_row_body(rows))
    addmore_body["WS_CSRFTOKEN"] = listing["csrf"]

    log.info(f"WS IA create — step A (addmore) with {len(rows)} existing rows")
    # allow_redirects=False so a 302→login is visible (otherwise we'd
    # silently follow the redirect and parse the login page as the
    # 'addmore response', confusing the diagnostic chain).
    r_add = session.post(post_url, data=addmore_body,
                         headers=addmore_headers, timeout=60,
                         allow_redirects=False)
    # Don't raise_for_status on 3xx — we want to inspect the redirect.
    if r_add.status_code >= 400:
        r_add.raise_for_status()

    new_rows, new_csrf, new_total = _parse_ia_rows_from_html(r_add.text)
    log.info(f"WS IA create — addmore response: {len(new_rows)} rows "
             f"(totalRows hidden={new_total}, csrf_changed={new_csrf != listing['csrf']})")

    if len(new_rows) != len(rows) + 1:
        # Defensive diagnostic: dump enough state into the log that we
        # don't have to round-trip through Kudu to debug. Every WS POST
        # failure mode (auth bounce / perm-denied / form-CSRF reject)
        # has a distinct fingerprint in the response, headers, and the
        # cookies we sent — capture all three so the operator's next
        # retry produces a self-contained log entry.
        _resp_status = r_add.status_code
        _resp_loc    = r_add.headers.get("Location") or ""
        _resp_ct     = r_add.headers.get("Content-Type") or ""
        _resp_text   = r_add.text or ""
        _is_login    = ("j_spring_security_check" in _resp_text
                        or "WealthSpectrum Login" in _resp_text
                        or "/loginScreen.do" in _resp_loc
                        or "/auth.do" in _resp_loc)
        _is_unauth   = "Requested resource unauthorized" in _resp_text
        # Cookie-shape diagnostic: 302 → /auth.do without a Set-Cookie
        # to invalidate JSESSIONID typically means WS dropped the
        # session because of a header/CSRF check (not because the
        # session genuinely expired). Set-Cookie WITH a new JSESSIONID
        # → WS rotated the session and we should refresh; Set-Cookie
        # clearing JSESSIONID → WS killed the session deliberately.
        _set_cookie_hdrs = r_add.headers.get_list("Set-Cookie") \
            if hasattr(r_add.headers, "get_list") \
            else [r_add.headers.get("Set-Cookie")] if r_add.headers.get("Set-Cookie") else []
        # List form (not dict) so duplicate cookie names remain visible
        # — collapsing to a dict would hide the very bug this log is
        # designed to catch (two JSESSIONIDs at different paths).
        _sent_cookies = [
            f"{c.name}@{c.domain}{c.path}=" +
            ((c.value[:8] + "…") if c.value and len(c.value) > 12 else (c.value or ""))
            for c in session.cookies
        ]
        _resp_headers = dict(r_add.headers)
        _sent_keys   = sorted(addmore_body.keys())
        _row_count   = sum(1 for k in _sent_keys
                           if k.startswith("categoryListBean["))
        _csrf_sent   = (addmore_body.get("WS_CSRFTOKEN") or "")[:12] + "…"
        log.warning(
            f"WS IA addmore failed:\n"
            f"  status={_resp_status} location={_resp_loc!r} content-type={_resp_ct!r}\n"
            f"  fingerprint: login_page={_is_login} unauth_page={_is_unauth}\n"
            f"  CSRF sent: {_csrf_sent} (from listing GET)\n"
            f"  session cookies sent (truncated): {_sent_cookies}\n"
            f"  response Set-Cookie headers: {_set_cookie_hdrs}\n"
            f"  response headers: {_resp_headers}\n"
            f"  body keys sent ({len(_sent_keys)}): "
            f"{[k for k in _sent_keys if not k.startswith('categoryListBean[')]} "
            f"+ {_row_count} categoryListBean[*] fields\n"
            f"  request headers: {dict(addmore_headers)}\n"
            f"  response head (first 800 chars): "
            f"{_resp_text[:800].replace(chr(10), ' ')!r}"
        )
        _dump_dir = Path(os.environ.get("KEYSTONE_DATA_DIR") or
                         str(Path(__file__).parent)) / "data" / "ws_pool_creator_probe"
        _dump_dir.mkdir(parents=True, exist_ok=True)
        _dump_addmore = _dump_dir / f"ia_addmore_{ia_code}.html"
        try: _dump_addmore.write_text(r_add.text, encoding="utf-8", errors="replace")
        except Exception: pass
        return {"status": "addmore_failed",
                "expected_rows": len(rows) + 1,
                "got_rows":      len(new_rows),
                "http_status":   _resp_status,
                "redirect_to":   _resp_loc,
                "is_login_page": _is_login,
                "is_unauth":     _is_unauth,
                "response_dump": str(_dump_addmore),
                "session": session, "base": base}

    # Populate the new (last) row with our values
    new_rows[-1] = {
        "option_value": ia_code.strip(),
        "option_desc":  ia_name.strip(),
        "option_seq":   str(int(ia_no)),
        "default":      False,
    }

    # ── Step B: POST mode=update with all N+1 rows — actually persists.
    # Body shape verbatim from operator's UI cURL capture of the Save
    # click:  mode, menuDisp, totalRows, categoryListBean[0..N].*,
    # WS_CSRFTOKEN (CSRF at the END). Browser does NOT send btnSave on
    # this click — Struts reads the persist intent from mode=update.
    # Same WS_CSRFTOKEN value as Step A (WS does not rotate the token
    # between addmore and update — confirmed in the operator's capture).
    update_body: dict = {
        "mode":         "update",
        "menuDisp":     "null",
        "totalRows":    str(len(new_rows)),
    }
    update_body.update(_ia_row_body(new_rows))
    update_body["WS_CSRFTOKEN"] = new_csrf or listing["csrf"]

    # Step A's response may have added another JSESSIONID at a
    # different path (Set-Cookie from the addmore re-render); dedupe
    # again so Step B's Cookie header carries a single session id.
    _removed_b = _dedupe_session_cookies(session)
    if _removed_b:
        log.info(f"WS IA create — deduped {_removed_b} duplicate JSESSIONID(s) "
                 f"before Step B")

    log.info(f"WS IA create — step B (update) posting {len(new_rows)} rows "
             f"(new: {ia_code} / seq {ia_no})")
    r_upd = session.post(post_url, data=update_body,
                         headers=update_headers, timeout=60,
                         allow_redirects=False)
    if r_upd.status_code >= 400:
        r_upd.raise_for_status()

    _dump_dir = Path(os.environ.get("KEYSTONE_DATA_DIR") or
                     str(Path(__file__).parent)) / "data" / "ws_pool_creator_probe"
    _dump_dir.mkdir(parents=True, exist_ok=True)
    _dump_path = _dump_dir / f"ia_post_{ia_code}.html"
    try: _dump_path.write_text(r_upd.text, encoding="utf-8", errors="replace")
    except Exception: pass
    log.info(f"WS IA create — step B response status={r_upd.status_code} "
             f"location={r_upd.headers.get('Location') or ''!r} "
             f"len={len(r_upd.text)} → {_dump_path}")
    # Step B fingerprint log mirroring Step A's so we have the full
    # request/response shape if the verify below misses. Cheap to log
    # always — if verify finds the new IA, the WARN is just noise.
    if r_upd.status_code >= 300 or not r_upd.text:
        _set_b = r_upd.headers.get_list("Set-Cookie") \
            if hasattr(r_upd.headers, "get_list") \
            else ([r_upd.headers.get("Set-Cookie")] if r_upd.headers.get("Set-Cookie") else [])
        _cookies_b = [
            f"{c.name}@{c.domain}{c.path}=" +
            ((c.value[:8] + "…") if c.value and len(c.value) > 12 else (c.value or ""))
            for c in session.cookies
        ]
        log.warning(
            f"WS IA Step B suspicious response:\n"
            f"  status={r_upd.status_code} location={r_upd.headers.get('Location')!r}\n"
            f"  session cookies sent: {_cookies_b}\n"
            f"  response Set-Cookie: {_set_b}\n"
            f"  response head (first 800 chars): "
            f"{r_upd.text[:800].replace(chr(10), ' ')!r}"
        )

    # Verify
    verify = ws_list_investment_approaches(session=session, base=base)
    log.info(f"WS IA create — verify list size: {len(verify['rows'])} rows")
    for row in verify["rows"]:
        if row["option_value"].strip().upper() == code_target:
            return {"status": "created", "row": row,
                    "session": session, "base": base}

    text_lc = r_upd.text.lower()

    # WS sometimes returns a 'Welcome <user>! … Requested resource
    # unauthorized' chrome on Step B. Operator confirmed this is NOT a
    # permissions issue on the keystone account, so don't tag it as
    # such — surface the response as 'ws_returned_unauthorized' with
    # the dump file so we can iterate on the real cause (likely
    # another wire-shape mismatch the diagnostic log will surface).
    if 'requested resource unauthorized' in text_lc or 'unauthorized' in text_lc[:5000]:
        return {
            "status": "ws_returned_unauthorized",
            "message": (
                "WS Step B returned the 'Requested resource unauthorized' "
                "chrome. Per operator: not a permission issue on the "
                "keystone account, so the cause is somewhere in the "
                "request shape (cookies / CSRF / headers / body). "
                "See the diagnostic WARN log just above for the full "
                "request/response fingerprint."
            ),
            "http_status":       r_upd.status_code,
            "response_dump":     str(_dump_path),
            "verify_row_count":  len(verify['rows']),
            "session": session, "base": base,
        }

    signals = []
    if "maker" in text_lc and "checker" in text_lc:   signals.append("maker-checker")
    if "authoriz" in text_lc or "approval" in text_lc: signals.append("approval")
    if "error" in text_lc and "success" not in text_lc: signals.append("error-text")
    if "successfully" in text_lc or "saved" in text_lc: signals.append("success-text")

    return {"status": "unknown", "response_len": len(r_upd.text),
            "response_dump": str(_dump_path),
            "signals": signals,
            "verify_row_count": len(verify['rows']),
            "session": session, "base": base}


def _scrape_select_options(html: str, select_name: str) -> list:
    """Return [{'value','label'}] for a <select name="…">. Blank-value
    placeholder options (the '[-Select Any-]' sentinel) are skipped."""
    soup = BeautifulSoup(html, "html.parser")
    sel = soup.find("select", {"name": select_name})
    if not sel:
        return []
    out = []
    for opt in sel.find_all("option"):
        v = (opt.get("value") or "").strip()
        if not v:
            continue
        out.append({"value": v, "label": opt.get_text(strip=True)})
    return out


def _get_scheme_form_html(session, base) -> str:
    r = session.get(f"{base}/{SCHEME_FORM_URL}", timeout=30)
    r.raise_for_status()
    return r.text


def ws_list_fund_managers(session=None, base=None,
                          auth_cache_path: str = '') -> list:
    """Return fundmgrid options — [{'value','label'}] where value is the
    numeric fund-manager id and label is typically 'Firstname Lastname - <id>'."""
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)
    return _scrape_select_options(_get_scheme_form_html(session, base), "fundmgrid")


def ws_list_strategies(session=None, base=None,
                       auth_cache_path: str = '') -> list:
    """Return strategycode options — EQUITY / DEBT / HYBRID / OTHERS."""
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)
    return _scrape_select_options(_get_scheme_form_html(session, base), "strategycode")


def ws_list_depositories(session=None, base=None,
                         auth_cache_path: str = '') -> list:
    """Return [{'dp','dpid','dp_name','sebi'}] from the DP picklist popup
    (protected/dpPickList.jsp). Used for the Pool-Master custodian selection
    — we bypass the UI popup by POSTing dp+dpid directly."""
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)
    r = session.get(f"{base}/{DP_PICKLIST_URL}", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Each data row: <tr><td><a href="javascript:PickList('forms[0]','NSDL','IN301348')">NSDL</a></td>
    # <td>National Securities Depository Limited</td><td>IN301348</td><td>ICICI BANK LTD</td><td>IN/CUS/005</td></tr>
    rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        a = tds[0].find("a")
        if not a:
            continue
        href = a.get("href", "") or ""
        m = re.search(r"PickList\(\s*['\"]forms\[0\]['\"]\s*,\s*['\"]([^'\"]+)['\"]"
                      r"\s*,\s*['\"]([^'\"]+)['\"]\s*\)", href)
        if not m:
            continue
        rows.append({
            "dp":       m.group(1),
            "dpid":     m.group(2),
            "dep_name": tds[1].get_text(strip=True),
            "dp_name":  tds[3].get_text(strip=True),
            "sebi":     tds[4].get_text(strip=True),
        })
    return rows


def ws_create_scheme(scheme_name: str,
                    start_date: str,             # dd/MM/yyyy
                    fund_manager_id: str,        # numeric id from fundmgrid
                    ia_code: str,                # matches option_value in IA list
                    strategy_code: str,          # EQUITY|DEBT|HYBRID|OTHERS
                    session=None, base=None,
                    auth_cache_path: str = '') -> dict:
    """POST a new scheme to editSchemeMaster.do with mode=insert.

    The Save button's onclick runs finalcheck('insert') which sets the
    form's mode field from 'create' → 'insert' before submit. We skip
    the JS and POST mode=insert directly.
    """
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)

    form_html = _get_scheme_form_html(session, base)
    soup = BeautifulSoup(form_html, "html.parser")
    csrf_inp = soup.find("input", {"name": "WS_CSRFTOKEN"})
    csrf = (csrf_inp.get("value") if csrf_inp else "") or ""

    body = {
        "mode":              "insert",
        "mode1":             "",
        "menuDisp":          "",
        "format":            "dd/MM/yyyy",
        "firmflag":          "N",
        "exist":             "N",
        "DtlType":           "m_LIQUID",
        "symtype":           "m",
        "liquidOrderGen":    "N",
        "sdt":               "",
        "cdt":               "",
        "firmid":            "0",
        "schemeid":          "0",
        "schemename":        scheme_name.strip(),
        "startdate":         start_date.strip(),
        "closedate":         "",
        "fundmgrid":         str(fund_manager_id).strip(),
        "pvflag":            "V",
        "minbalance":        "0.00",
        "minInvestmentAmt":  "0.00",
        "firstDrawdownPer":  "0.0",
        "minRedAmt":         "0.00",
        "minSubAmt":         "0.00",
        "minAddSubAmt":      "0.00",
        "maxHoldingFlag":    "N",
        "maxHoldingPerc":    "0.00",
        "riskRating":        "",
        "iacode":            ia_code.strip(),
        "strategycode":      strategy_code.strip(),
        "refcode1": "", "refcode2": "", "refcode3": "", "refcode4": "",
        "refcode5": "", "refcode6": "", "refcode7": "", "refcode8": "",
        "refcode9": "", "refcode10": "",
        "notes":             "",
        "save":              "Save",
        "WS_CSRFTOKEN":      csrf,
    }
    log.info(f"WS scheme create — {scheme_name!r} under IA {ia_code} "
             f"/ fund mgr {fund_manager_id} / strategy {strategy_code}")
    resp = session.post(f"{base}/{SCHEME_POST_URL}", data=body,
                        headers={"Referer": f"{base}/{SCHEME_FORM_URL}"},
                        timeout=60)
    resp.raise_for_status()
    log.info(f"WS scheme create — response {len(resp.text)} bytes")
    return {"status": "posted", "response_text": resp.text,
            "session": session, "base": base}


def ws_create_pool(dp: str, dpid: str, dp_client_id: str,
                   scheme_mapin_id: str, description: str,
                   custody_scheme_code: str,
                   session=None, base=None,
                   auth_cache_path: str = '') -> dict:
    """POST a new Pool Master entry to editAccountScheme.do with mode=save.

    The form has `dpid` rendered as disabled; the DP PickList popup re-enables
    it (WS convention — see reference_ws_portal_ajax_patterns.md) and sets
    both dp+dpid. We bypass the picker and send both values directly.
    """
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)

    r0 = session.get(f"{base}/{POOL_FORM_URL}", timeout=30)
    r0.raise_for_status()
    soup = BeautifulSoup(r0.text, "html.parser")
    csrf_inp = soup.find("input", {"name": "WS_CSRFTOKEN"})
    csrf = (csrf_inp.get("value") if csrf_inp else "") or ""

    body = {
        "MFIndRecords":      "0",
        "actTypeS":          "0",
        "totalNumOfClients": "0",
        "mode":              "save",    # setMode() mutates from 'create' → 'save'
        "oldmode":           "",
        "issuerName":        "",
        "folio":             "",
        "activeFlag":        "",
        "oldFolio":          "",
        "oldIssuer":         "",
        "newFolio":          "",
        "newIssuer":         "",
        "install_switch":    "GOLDSTANDARD",
        "poolFolioFlag":     "S",
        "dp":                dp.strip(),
        "dpid":              dpid.strip(),
        "dpClientId":        dp_client_id.strip(),
        "schemeMapinId":     scheme_mapin_id.strip(),
        "remarks":           description.strip(),
        "refCode1":          "",
        "refCode2":          "",    # PAN (encrypted/masked on form — leave empty for pools)
        "refCode3":          "",
        "refCode4":          "",
        "refCode5":          "",
        "refCode6":          custody_scheme_code.strip(),
        "refCode7":          "",
        "refCode8":          "",
        "refCode9":          "",
        "refCode10":         "",
        "folioFlag":         "N",
        "folioFlagOri":      "N",
        "folioFlagDb":       "N",
        "noofrows":          "2",
        "detailRecords":     "0",
        "firmflag":          "N",
        "sbt":               "Save",
        "WS_CSRFTOKEN":      csrf,
    }
    # Empty AMC/folio sub-rows (2 row default; all blank on create)
    for i in range(2):
        body[f"issuer[{i}]"]     = ""
        body[f"issuername[{i}]"] = ""
        body[f"foliono[{i}]"]    = ""
        body[f"oldActive[{i}]"]  = "false"

    log.info(f"WS pool create — dp={dp} dpid={dpid} dpClientId={dp_client_id} "
             f"schemeMapinId={scheme_mapin_id}")
    resp = session.post(f"{base}/{POOL_POST_URL}", data=body,
                        headers={"Referer": f"{base}/{POOL_FORM_URL}"},
                        timeout=60)
    resp.raise_for_status()
    log.info(f"WS pool create — response {len(resp.text)} bytes")
    return {"status": "posted", "response_text": resp.text,
            "session": session, "base": base}


# ── Bank Master create ──────────────────────────────────────────────────── #
#
# editBankMaster.do?mode=create renders one row of Bank Master fields. The
# Save button calls finalcheck('update') which (a) flips mode to 'update'
# and (b) AES-encrypts bankacid[0] in-place using values from two hidden
# fields on the page (`_ref1` = username, `_ref2` = form-render timestamp).
# We replicate that encryption headlessly below.
#
# Encryption parameters (from encryption.js + AESUtil.js):
#   key_str = username + timestamp.replace(' ', '', 1)   # one space only
#   salt    = utf-8 bytes of username
#   iv      = first 16 bytes of SHA256(username)
#   aes_key = PBKDF2-HMAC-SHA1(key_str, salt, length=16, iterations=100)
#   ct      = AES-128-CBC(plaintext, aes_key, iv) with PKCS#7 padding
#   wire    = ct.hex()
#
# Submitted as the bare bankacid[0] value; the server side decrypts using
# the same parameters before persisting.

BANK_MASTER_FORM_URL = "editBankMaster.do?mode=create"
BANK_MASTER_POST_URL = "editBankMaster.do"

BANK_AC_TYPE_CODES = {
    "savings":     "S",
    "saving":      "S",
    "current":     "C",
    "collection":  "COLLECT",
    "collect":     "COLLECT",
    "operative":   "OPERATIV",
    "operativ":    "OPERATIV",
    "investment":  "INVEST",
    "invest":      "INVEST",
}


def _ws_encrypt_field(plaintext: str, username: str, timestamp: str) -> str:
    """Field-level WS encryption for hidden inputs that the portal expects
    pre-encrypted (e.g. Bank Master `bankacid`). Mirrors AESUtil.encryptWS
    with the deterministic salt/iv/key derived from the username + the
    page-render timestamp baked into _ref1 / _ref2.

    Returns ciphertext as a hex string.
    """
    import hashlib as _hashlib
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import hashes as _hashes, padding as _padding
    from cryptography.hazmat.backends import default_backend

    # JS: key2.replace(' ', '') — replaces only the FIRST space.
    key_str = username + timestamp.replace(" ", "", 1)
    salt    = username.encode("utf-8")
    iv      = _hashlib.sha256(username.encode("utf-8")).digest()[:16]

    kdf = PBKDF2HMAC(algorithm=_hashes.SHA1(), length=16,
                     salt=salt, iterations=100, backend=default_backend())
    key = kdf.derive(key_str.encode("utf-8"))

    padder = _padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    enc = cipher.encryptor()
    ct  = enc.update(padded) + enc.finalize()
    return ct.hex()


def ws_create_bank_master(bank_code: str, bank_account_id: str,
                          bank_name: str, bank_account_name: str = "",
                          account_type: str = "S",
                          mapin: str = "", cash_symbol: str = "CASH",
                          ref_code1: str = "", ref_code2: str = "",
                          session=None, base=None,
                          auth_cache_path: str = '') -> dict:
    """POST a new Bank Master row to editBankMaster.do.

    Replicates the browser's Save flow:
      1. GET the create form to grab WS_CSRFTOKEN + the _ref1/_ref2 pair
         used to derive the field-encryption key.
      2. AES-encrypt the bank account id (the only encrypted field on
         this form — see encryption.js / encryptWSData).
      3. POST mode=update with the rest of the row's hidden + visible
         fields. The form's onsubmit JS flips mode 'create' → 'update'.

    Args:
      bank_code:         8-char unique code (Pool MAPIN works well here).
      bank_account_id:   Account number (encrypted before submit).
      bank_name:         Free text, max 60 chars.
      bank_account_name: Sole/first holder name from CML, max 60 chars.
      account_type:      One of S, C, COLLECT, OPERATIV, INVEST. Accepts
                         human labels too — see BANK_AC_TYPE_CODES.
      mapin:             Optional MAPIN to attach to the bank record.
      cash_symbol:       Defaults to CASH (the only option on the form).
      ref_code1, ref_code2: Optional free-text reference fields.

    Returns: {status, http_status, response_text, session, base}
    """
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)

    if not bank_code or not bank_account_id or not bank_name:
        raise ValueError("bank_code, bank_account_id and bank_name are required")

    # Translate human-friendly account-type values into the dropdown code.
    actype = (account_type or "").strip()
    if actype.lower() in BANK_AC_TYPE_CODES:
        actype = BANK_AC_TYPE_CODES[actype.lower()]
    if actype not in {"S", "C", "COLLECT", "OPERATIV", "INVEST"}:
        raise ValueError(f"Unknown account_type {account_type!r}; "
                         f"expected one of S/C/COLLECT/OPERATIV/INVEST or a label")

    # 1. Fetch the form to extract CSRF + the encryption ref values.
    r0 = session.get(f"{base}/{BANK_MASTER_FORM_URL}", timeout=30)
    r0.raise_for_status()
    soup = BeautifulSoup(r0.text, "html.parser")

    csrf_inp = soup.find("input", {"name": "WS_CSRFTOKEN"})
    csrf = (csrf_inp.get("value") if csrf_inp else "") or ""

    ref1_inp = soup.find("input", {"id": "_ref1"})
    ref2_inp = soup.find("input", {"id": "_ref2"})
    if not ref1_inp or not ref2_inp:
        raise RuntimeError("editBankMaster form is missing _ref1 / _ref2 — "
                           "field encryption cannot be derived")
    username  = (ref1_inp.get("value") or "").strip()
    timestamp = (ref2_inp.get("value") or "")

    # 2. Encrypt the account id exactly as the browser would.
    enc_acid = _ws_encrypt_field(bank_account_id.strip(), username, timestamp)

    # 3. POST with mode=update (the JS flips it from 'create' on submit).
    body = {
        "mode":              "update",
        "menuDisp":          "",
        "symtype":           "c",
        "bankid[0]":         "0",
        "bankcode[0]":       bank_code.strip(),
        "bankacid[0]":       enc_acid,
        "bankname[0]":       bank_name.strip(),
        "bankacname[0]":     bank_account_name.strip(),
        "bankactype[0]":     actype,
        "cashsymbolcode[0]": cash_symbol.strip() or "CASH",
        "mapin[0]":          mapin.strip(),
        "refcode1[0]":       ref_code1.strip(),
        "refcode2[0]":       ref_code2.strip(),
        "save":              "Save",
        "WS_CSRFTOKEN":      csrf,
    }

    log.info(f"WS bank master create — code={bank_code} bank={bank_name} "
             f"actype={actype} mapin={mapin}")
    resp = session.post(f"{base}/{BANK_MASTER_POST_URL}", data=body,
                        headers={"Referer": f"{base}/{BANK_MASTER_FORM_URL}"},
                        timeout=60)
    resp.raise_for_status()
    log.info(f"WS bank master create — HTTP {resp.status_code}, "
             f"{len(resp.text)} bytes")
    return {
        "status":        "posted",
        "http_status":   resp.status_code,
        "response_text": resp.text,
        "session":       session,
        "base":          base,
    }


# ============================================================================ #
# ── Benchmark assignment ────────────────────────────────────────────────── #
#
# After a scheme is created, the benchmark for that scheme is set via a
# 2-step flow (the second page also has a Save, but the portfolio
# benchmark is persisted at Next-time — confirmed against ws_probe_ia_create.py
# Phase 7). We replicate just the Next step since that's what actually
# saves the scheme→benchmark link with weight.
#
# Field shape comes from the operator's captured browser cURL — the
# weight field is bmratioc[0] (NOT weight[0]); bmseqc[0]=1 is required;
# CSRF is stable across both POSTs in this flow.

BENCHMARK_LIST_QUERY_URL = "editSchemeMaster.do?mode=query"
BENCHMARK_LIST_POST_URL  = "editSchemeMaster.do"
BENCHMARK_ALLOC_URL      = "editBenchmarkAlloc.do"


def ws_lookup_scheme_id(scheme_name: str,
                         session=None, base=None,
                         auth_cache_path: str = '') -> str:
    """Look up the scheme_id (numeric, scope-id format) for a scheme by
    name. Uses the editSchemeMaster.do?mode=query → mode=list flow.

    Returns the scheme_id as a string, or '' if not found.
    """
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)

    # 1. GET the query form to grab CSRF.
    r0 = session.get(f"{base}/{BENCHMARK_LIST_QUERY_URL}", timeout=30,
                     allow_redirects=False)
    r0.raise_for_status()
    soup0 = BeautifulSoup(r0.text, "html.parser")
    csrf_inp = soup0.find("input", {"name": "WS_CSRFTOKEN"})
    csrf = (csrf_inp.get("value") if csrf_inp else "") or ""

    # 2. POST mode=list with schemename filter -> the list table.
    list_body = {
        "format":       "dd/MM/yyyy",
        "mode":         "list",
        "mode1":        "",
        "menuDisp":     "",
        "firmflag":     "N",
        "scope":        "S",
        "sdt":          "",
        "cdt":          "",
        "tmpschemeid":  "",
        "schemeid":     "",
        "schemename":   scheme_name,
        "firmid":       "0",
        "fundmgrid":    "0",
        "query":        "Query",
        "WS_CSRFTOKEN": csrf,
    }
    r = session.post(f"{base}/{BENCHMARK_LIST_POST_URL}", data=list_body,
                     headers={"Referer": f"{base}/{BENCHMARK_LIST_QUERY_URL}"},
                     timeout=30, allow_redirects=False)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # Match the row whose name cell contains the scheme name; pull a
    # numeric id from the cells or from any onclick scopeid=N.
    target = scheme_name.strip().lower()
    for tr in soup.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if any(target in (c or "").lower() for c in cells):
            for txt in cells:
                if (txt or "").isdigit():
                    return txt
            for tag in tr.find_all(True):
                for attr in ("onclick", "href"):
                    v = tag.get(attr) or ""
                    m = re.search(r"scopeid=(\d+)", v)
                    if m:
                        return m.group(1)
    return ''


def ws_assign_benchmark(scheme_id: str,
                         index_code: str,
                         index_name: str,
                         weight_pct: str = "100.0",
                         session=None, base=None,
                         auth_cache_path: str = '') -> dict:
    """Assign a benchmark to a scheme via the editBenchmarkAlloc Next POST.

    The portfolio-level benchmark is persisted by the Next click — the
    second-page Save is for the asset-class allocation detail and is
    optional for our use case (single benchmark @ 100%). Confirmed via
    ws_probe_ia_create.py Phase 7.

    Args:
      scheme_id:  numeric scheme id (use ws_lookup_scheme_id to get it).
      index_code: benchmark short code (e.g. 'MULTIASSET2'). Must match
                  one of the picklist's index codes.
      index_name: benchmark display name (e.g. 'NSE Multi Asset Index 2').
                  Sent as bmnamec[0] for the round-trip — server uses
                  bmtypec[0] as the FK and re-renders bmnamec empty,
                  but the wire shape requires it.
      weight_pct: weight as a string (default '100.0').

    Returns:
      {status, http_status, response_text, session, base}
      status is 'assigned' if the POST returned 200 + the alloc form
      shows bmtypec[0]=index_code on a follow-up GET; 'unclear' if
      response was 200 but verification didn't confirm; 'error' if
      WS bounced the request.
    """
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)

    if not scheme_id or not index_code:
        raise ValueError("scheme_id and index_code are required")

    alloc_url = (f"{base}/{BENCHMARK_ALLOC_URL}?mode=modify"
                 f"&scope=S&scopeid={scheme_id}&menuDisp=N")

    # Step 1: GET the alloc form. This populates the per-asset-class
    # hidden fields the Next POST needs to round-trip, plus gives us
    # the CSRF.
    r1 = session.get(alloc_url, timeout=30, allow_redirects=False)
    r1.raise_for_status()
    soup = BeautifulSoup(r1.text, "html.parser")
    csrf_inp = soup.find("input", {"name": "WS_CSRFTOKEN"})
    csrf = (csrf_inp.get("value") if csrf_inp else "") or ""

    # Build the body from every existing input on the form so the
    # asset-class scaffolding (allocPercentage, astname, astcls, ...)
    # gets echoed back exactly as the server expects. Then overlay
    # our four picker fields for slot 0.
    body: dict = {}
    for inp in soup.find_all("input"):
        nm = inp.get("name")
        if not nm:
            continue
        if (inp.get("type") or "").lower() in ("submit", "reset", "button"):
            continue
        body[nm] = inp.get("value", "") or ""

    body["bmtypec[0]"]  = index_code.strip()
    body["bmnamec[0]"]  = index_name.strip()
    body["bmratioc[0]"] = str(weight_pct).strip()
    body["bmseqc[0]"]   = "1"
    body.pop("weight[0]", None)        # not in the wire shape
    body["submit"]      = "Next"
    body["WS_CSRFTOKEN"] = csrf

    log.info(f"WS benchmark assign — scheme {scheme_id} -> "
             f"{index_code} @ {weight_pct}%")
    r2 = session.post(f"{base}/{BENCHMARK_ALLOC_URL}", data=body,
                      headers={"Referer": alloc_url},
                      timeout=60, allow_redirects=False)
    log.info(f"WS benchmark assign — POST status={r2.status_code} "
             f"len={len(r2.text)} location={r2.headers.get('Location')!r}")

    # Verify by re-fetching the alloc form and checking bmtypec[0].
    rv = session.get(alloc_url, timeout=30, allow_redirects=False)
    soup_v = BeautifulSoup(rv.text, "html.parser")
    bmtypec0 = soup_v.find("input", {"name": "bmtypec[0]"})
    persisted_code = (bmtypec0.get("value") if bmtypec0 else "") or ""

    if persisted_code.strip().upper() == index_code.strip().upper():
        status = "assigned"
        log.info(f"WS benchmark assign — confirmed: scheme {scheme_id} "
                 f"now has benchmark {index_code}")
    else:
        status = "unclear"
        log.warning(f"WS benchmark assign — POST returned {r2.status_code} "
                    f"but bmtypec[0]={persisted_code!r} (expected {index_code!r})")
    return {
        "status":         status,
        "http_status":    r2.status_code,
        "response_text":  r2.text,
        "persisted_code": persisted_code,
        "session":        session,
        "base":           base,
    }


# ============================================================================ #
# ── Morning BOD pipeline ─────────────────────────────────────────────────── #
# 4-stage process replicating the Operations team's morning routine:
#   2) Price upload (3-step: choose-file → mapid+source → trigger processing)
#   4) Asset Performance Calculation
#   5) BoD batch — Cash & Eq.
#   6) BoD batch — Saleable holding
#
# All three bridges share the same session helpers as the pool-creation
# bridge above. See ws_probe_bod.py for the form-inspection pass that
# discovered the action URLs and field names below.
# ============================================================================ #

PRICE_UPLOAD_FORM_URL  = ("redirect.do?target=reportPricePosting&scope=*"
                          "&cmScope=*&consolidation=C&srcMenuId=1073")
PRICE_UPLOAD_POST_URL  = "protected/reportPricePosting.jsp?srcMenuId=1073"
PRICE_QUERY_POST_URL   = "queryPricePosting.do"
PRICE_WAIT_URL         = "protected/withMenu.jsp?self=genericWait.jsp"

ASSET_PERF_FORM_URL    = ("redirect.do?target=reportAssetPerfCalc&scope=*"
                          "&cmScope=*&consolidation=C&srcMenuId=1521")
ASSET_PERF_POST_URL    = "reportAssetPerfCalc.do"
ASSET_PERF_QUEUE_URL   = "perfQueueDisplay.do"

BOD_BATCH_FORM_URL     = ("redirect.do?target=bod&scope=*&cmScope=*"
                          "&consolidation=C&srcMenuId=1589")
BOD_BATCH_POST_URL     = "beginofDayBatchJob.do"

# Process dropdown values on the BoD batch form (verified against the
# live form HTML — see commit message for the full <select> dump).
BOD_PROCESS_VALUES = {
    "cash":                  "O",   # Cash & Eq.
    "saleable":              "S",   # Saleable holding
    "order_exec_carry":      "D",   # Order execution reconciliation and carry forward orders
    "order_exec_reversal":   "DR",  # Order execution reconciliation reversal
    "carry_forward_reversal":"C",   # Carry forward reversal (not currently driven by EoD; available for future use)
}


def _csrf(soup: BeautifulSoup) -> str:
    inp = soup.find("input", {"name": "WS_CSRFTOKEN"})
    return (inp.get("value") if inp else "") or ""


def ws_upload_price_file(file_path: str | Path, *,
                         mapid: int = 46,
                         source: str = "MFVR_NAV_map",
                         date_format: str = "dd/MM/yyyy",
                         session=None, base=None,
                         auth_cache_path: str = '',
                         wait_for_completion: bool = True,
                         poll_timeout_sec: int = 600,
                         progress_cb=None,
                         debug_dump_dir: str | Path | None = None) -> dict:
    """Upload a price/NAV file to WS via the 3-step Price Upload flow.

    Mirrors the operator clicks the video shows:

      1. GET the Choose-File form, multipart-POST the CSV to
         protected/reportPricePosting.jsp?srcMenuId=1073. The response
         is the next page (reportPricePosting.jsp) with a `tempfile`
         hidden input giving the server-side temp filename.
      2. POST queryPricePosting.do with mode=checkDuplicateFile and the
         mapid/source/file fields. The server validates and queues the
         processing job.
      3. POST queryPricePosting.do again with mode=errorPage to actually
         trigger processing (this is the equivalent of clicking the
         blue arrow on the genericWait page).

    The Total/Processed/Parsing-Error counts visible in the video at
    frame 22 only appear on the success-detail page reached by clicking
    the blue arrow on genericWait.jsp — NOT on the response from the
    kick-off POST. So we don't try to parse them here. Instead, when
    ``wait_for_completion=True`` (default), this function polls
    genericWait.jsp until the "Price Upload - Mapid: <mapid>" row whose
    Start time is at or after our submit moment carries the success
    icon. ok=True is set only when that completion row appears.

    Returns: {ok, completed, timed_out, filename, mapid, duplicate,
              http_status, response_text, last_observed, session, base}
    """
    from datetime import datetime as _dt
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)

    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"NAV file not found: {file_path}")

    # ── Step 1: GET the Choose-File form. Scrape ALL hidden inputs (not
    # just CSRF) — same as upload_0096. Some are server-generated state
    # that the multipart endpoint expects to see echoed back.
    log.info(f"WS price upload — GET form, mapid={mapid}, file={file_path.name}")
    r0 = session.get(f"{base}/{PRICE_UPLOAD_FORM_URL}", timeout=30)
    r0.raise_for_status()
    soup0 = BeautifulSoup(r0.text, "html.parser")
    form0 = soup0.find("form")
    if not form0:
        raise RuntimeError("No <form> on price-upload page")
    hidden0 = _scrape_all_inputs(r0.text)
    # Critical: `sname` carries the filename the operator picked. Empty
    # string here — which my prior version sent — leaves WS unable to
    # associate the staged temp file with the session.
    hidden0["sname"] = file_path.name

    # ── Step 2: multipart upload. MIME picked by extension so WS knows
    # how to parse — text/csv for .csv, vnd.ms-excel for .xls, etc.
    ext = file_path.suffix.lower()
    if ext == ".csv":
        mime = "text/csv"
    elif ext == ".xlsx":
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif ext == ".xls":
        mime = "application/vnd.ms-excel"
    else:
        mime = "application/octet-stream"

    log.info(f"WS price upload — multipart POST ({mime}), {file_path.stat().st_size} bytes")
    with file_path.open("rb") as fh:
        files = {"filepath": (file_path.name, fh, mime)}
        r1 = session.post(
            f"{base}/{PRICE_UPLOAD_POST_URL}",
            data=hidden0, files=files,
            headers={"Referer": f"{base}/{PRICE_UPLOAD_FORM_URL}"},
            timeout=180,
        )
    r1.raise_for_status()

    # Persist the response so we can see what the server returned if
    # the next step misbehaves. Same pattern as upload_0096.
    _dump_dir = Path(os.environ.get("KEYSTONE_DATA_DIR") or
                     str(Path(__file__).parent)) / "data" / "ws_upload_probe"
    _dump_dir.mkdir(parents=True, exist_ok=True)
    (_dump_dir / "price_step2_response.html").write_text(
        r1.text, encoding="utf-8", errors="replace")

    # The step-2 response IS the Price Posting form with mapid input
    # plus a hidden `tempfile` containing the server-side staged name.
    fields2 = _scrape_all_inputs(r1.text)
    server_tempfile = fields2.get("tempfile", "")
    if not server_tempfile:
        # WS didn't return the expected page — surface what came back.
        snippet = re.sub(r'\s+', ' ', r1.text[:500]).strip()
        raise RuntimeError(
            "Price upload step 2 did not return the Price Posting form "
            "(no `tempfile` field). First 500 chars of response: "
            + snippet)
    log.info(f"WS price upload — step 2 OK, tempfile={server_tempfile}")

    # Build the mapper-fields body from EVERY scraped input. The
    # browser would hand WS back the full form contents on submit;
    # missing any of these can leave the server-side state incomplete.
    def _mapper_body(mode: str, action_str: str) -> dict:
        out = dict(fields2)
        out["mapid"]          = str(mapid)
        out["mode"]           = mode
        out["actionString"]   = action_str
        out["source"]         = source
        out["format"]         = date_format
        out["refreshContent"] = "call"
        out["srcMenuId"]      = "1073"
        out["tempfile"]       = server_tempfile
        out["file"]           = server_tempfile
        return out

    # ── Step 3a: duplicate check (mode=checkDuplicateFile). The
    # response carries an `actionString` we MUST echo back in step 3b
    # for WS to actually run the mapper. Without it, the kick-off POST
    # is a no-op — which is what was happening before.
    log.info(f"WS price upload — checkDuplicateFile mapid={mapid} "
             f"file={server_tempfile}")
    r2 = session.post(f"{base}/{PRICE_QUERY_POST_URL}",
                      data=_mapper_body("checkDuplicateFile", ""),
                      headers={"Referer": f"{base}/{PRICE_UPLOAD_POST_URL}"},
                      timeout=60)
    r2.raise_for_status()
    fields3a = _scrape_all_inputs(r2.text)
    action_str = fields3a.get("actionString", "")
    if not action_str:
        m = re.search(r'name=["\']actionString["\'][^>]*value=["\']([^"\']*)',
                      r2.text)
        if m:
            action_str = m.group(1)
    log.info(f"WS price upload — duplicate check actionString={action_str!r}")
    if action_str == "nextWithCancel":
        return {
            "ok": False, "completed": False, "timed_out": False,
            "duplicate": True,
            "filename": server_tempfile, "mapid": mapid,
            "response_text": r2.text,
            "http_status": r2.status_code,
            "session": session, "base": base,
            "total": None, "processed": None, "errors": None,
            "last_observed": "WS rejected: file already uploaded "
                              "and duplicates are not allowed for this map",
        }
    # Echo the server's actionString verbatim — that's what the
    # browser's refreshInfo() / setExecWaitAction() does. The two
    # well-known values are:
    #   "next"               — fresh upload, no duplicate detected
    #   "nextWithOKContinue" — duplicate but operator overrode (we always
    #                          override automatically; see Force toggle)
    # Sending "nextWithOKContinue" when the server returned "next"
    # would falsely signal a duplicate-override decision and may make
    # WS look for an existing version to overwrite when none exists.
    if action_str not in ("next", "nextWithOKContinue"):
        log.warning(f"WS price upload — unexpected actionString "
                    f"{action_str!r}; aborting before kick-off rather "
                    f"than guessing")
        return {
            "ok": False, "completed": False, "timed_out": False,
            "duplicate": False,
            "filename": server_tempfile, "mapid": mapid,
            "session": session, "base": base,
            "error_message": f"unrecognised actionString from WS: {action_str!r}",
        }
    effective_action = action_str
    duplicate = action_str == "nextWithOKContinue"

    # ── Step 3b: run the mapper (mode=errorPage). Same field bag plus
    # the actionString from the duplicate check.
    #
    # Unlike trade-posting (mapid 96) where this XHR alone queues the
    # job, price-posting (mapid 46) appears to require BOTH the XHR
    # AND a follow-up form submit to genericWait.jsp — the browser's
    # setExecWaitAction() does both in sequence. The XHR validates and
    # returns the result page with counts; the form submit is what
    # actually triggers the server-side processing job to queue.
    # First request was returning counts but never showing in the
    # genericWait.jsp queue → operator confirmed via manual re-upload
    # that no duplicate warning fired (so the file never landed).
    submit_dt = _ist_naive_now()
    log.info(f"WS price upload — kick-off mapper mode=errorPage "
             f"actionString={effective_action!r}")
    body_kickoff = _mapper_body("errorPage", effective_action)
    r3 = session.post(f"{base}/{PRICE_QUERY_POST_URL}",
                      data=body_kickoff,
                      headers={"Referer": f"{base}/{PRICE_UPLOAD_POST_URL}"},
                      timeout=300)
    r3.raise_for_status()
    text = r3.text
    (_dump_dir / "price_step3b_response.html").write_text(
        text, encoding="utf-8", errors="replace")

    # Step 3c: form submit to genericWait.jsp — mimics the browser's
    # `document.forms[0].submit()` after retrieveURLForExeNWait.
    # Without this, the XHR validation completes (returning counts)
    # but the actual queue entry never gets created.
    log.info("WS price upload — form submit to genericWait.jsp "
             "(triggers actual queue)")
    try:
        r3c = session.post(f"{base}/{PRICE_WAIT_URL}",
                           data=body_kickoff,
                           headers={"Referer": f"{base}/{PRICE_UPLOAD_POST_URL}"},
                           timeout=60)
        r3c.raise_for_status()
        (_dump_dir / "price_step3c_response.html").write_text(
            r3c.text, encoding="utf-8", errors="replace")
    except Exception as _e:
        log.warning(f"WS price upload — wait-page submit failed: {_e}")
        # Don't fail the upload on this — the XHR may have already
        # done the queue work. Poll below will tell us.

    # Step 3b's response IS the result page in upload_0096's experience
    # for trade posting. Try to parse it inline; the poll below is a
    # belt-and-braces check for the price-posting variant.
    counts = _parse_posting_result(text)
    total      = counts.get("total")
    processed  = counts.get("processed")
    parse_err  = counts.get("parsing_errors")
    log.info(f"WS price upload — counts from step 3b: total={total} "
             f"processed={processed} parse_err={parse_err}")

    # Always poll genericWait.jsp for the row to mark Completed —
    # step 3b's counts are validation-only and don't prove the queue
    # actually accepted the job. Step 3c (the form submit just above)
    # is what triggers queuing; the row in genericWait.jsp is the
    # source of truth.
    poll: dict = {}
    if wait_for_completion:
        row_label = f"Price Upload - Mapid: {mapid}"
        poll = _ws_poll_for_completion(
            session, base,
            queue_url=PRICE_WAIT_URL,
            row_label=row_label,
            after_dt=submit_dt,
            timeout_sec=poll_timeout_sec,
            label_for_log=row_label,
            progress_cb=progress_cb,
        )
        ok = bool(poll.get("completed"))
    else:
        ok = r3.status_code < 400

    log.info(f"WS price upload — duplicate={duplicate} "
             f"step3b_counts=total={total}/processed={processed}/err={parse_err} "
             f"poll_completed={poll.get('completed')} "
             f"poll_timed_out={poll.get('timed_out')}")

    return {
        "ok":               ok,
        "completed":        poll.get("completed") if wait_for_completion else None,
        "timed_out":        poll.get("timed_out") if wait_for_completion else False,
        "duplicate":        duplicate,
        "filename":         server_tempfile,
        "mapid":            mapid,
        "total":            total,
        "processed":        processed,
        "errors":           parse_err,
        "poll_attempts":    poll.get("attempts"),
        "poll_elapsed_sec": poll.get("elapsed_sec"),
        "last_observed":    poll.get("last_observed", '') or
                            (f"step 3b counts: total={total} processed={processed}"
                             if inline_ok else ''),
        "response_text":    text,
        "http_status":      r3.status_code,
        "session":          session,
        "base":             base,
    }


def _ws_poll_for_completion(session, base: str, *,
                             queue_url: str,
                             row_label: str,
                             after_dt,
                             timeout_sec: int = 300,
                             poll_interval: float = 5.0,
                             label_for_log: str = '',
                             progress_cb=None) -> dict:
    """Poll a WS queue/wait page until the row matching `row_label` whose
    Start time is at or after `after_dt` carries the success icon.

    `queue_url` is e.g. ``perfQueueDisplay.do`` or
    ``protected/withMenu.jsp?self=genericWait.jsp``.

    Row layout (from probes):
        <tr class="changecolor4">
          <td>{label}</td>
          <td>{user_or_scope}</td>
          <td>{dd/MM/yyyy HH:MM}</td>
          ...
          <td><img alt="Completed" src="protected/images/success.bmp" ...></td>
        </tr>

    Status icon detected via either ``alt="Completed"`` or ``src=*success.bmp``.
    Returns: {ok, completed, timed_out, last_observed, attempts, elapsed_sec}.
    """
    import re as _re
    import time as _time
    from datetime import datetime as _dt

    log.info(f"WS poll — {label_for_log or row_label!r} on {queue_url} "
             f"(timeout {timeout_sec}s)")

    deadline = _time.time() + timeout_sec
    attempts = 0
    last_status = "no row found yet"
    start_clock = _time.time()
    label_lower = row_label.lower()

    # dd/MM/yyyy HH:MM only (the page never shows seconds in the status
    # table, so we compare to minute resolution). 24-hour clock.
    _ts_rx = _re.compile(r'\b(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})\b')

    while _time.time() < deadline:
        attempts += 1
        try:
            r = session.get(f"{base}/{queue_url}", timeout=20)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for tr in soup.find_all("tr"):
                tds = tr.find_all("td")
                if not tds:
                    continue
                first_text = " ".join(tds[0].get_text(" ", strip=True).split())
                if label_lower not in first_text.lower():
                    continue
                # Pick out the first dd/MM/yyyy HH:MM token in the row.
                row_dt = None
                for td in tds[1:]:
                    m = _ts_rx.search(td.get_text(" ", strip=True))
                    if m:
                        try:
                            row_dt = _dt.strptime(m.group(1), '%d/%m/%Y %H:%M')
                            break
                        except ValueError:
                            pass
                if row_dt is None:
                    continue
                # Slack: WS shows minute precision; allow 90s back-dating to
                # account for our submit-vs-server clock skew. Note that
                # submit_dt is naive datetime.now() in the worker process
                # (UTC on Azure) while row_dt is parsed as IST — caller
                # is expected to pass after_dt in the same timezone the
                # row renders in. We additionally tag rows older than 6h
                # behind submit as definitively stale.
                if (after_dt - row_dt).total_seconds() > 90:
                    continue
                # Found a recent enough row. Determine status from IMG
                # element attributes rather than full-row substring —
                # the previous "success.bmp" in row_html check matched a
                # template img that's ALWAYS in the row, even mid-
                # processing or on FAILED rows. Now: walk every IMG,
                # classify as completed / failed / pending based on its
                # actual src or alt.
                row_status = "pending"   # | "completed" | "failed"
                for img in tr.find_all("img"):
                    src = (img.get("src") or "").lower()
                    alt = (img.get("alt") or "").lower()
                    if "success.bmp" in src or alt == "completed":
                        row_status = "completed"
                        break
                    if ("error.bmp" in src or "fail" in src
                            or alt in ("failed", "error", "stop")):
                        row_status = "failed"
                        break
                if row_status == "completed":
                    return {
                        "ok":          True,
                        "completed":   True,
                        "timed_out":   False,
                        "last_observed": f"row at {row_dt.strftime('%d/%m/%Y %H:%M')} — Completed",
                        "attempts":    attempts,
                        "elapsed_sec": int(_time.time() - start_clock),
                    }
                if row_status == "failed":
                    return {
                        "ok":          False,
                        "completed":   False,
                        "timed_out":   False,
                        "failed":      True,
                        "last_observed": f"row at {row_dt.strftime('%d/%m/%Y %H:%M')} — FAILED",
                        "attempts":    attempts,
                        "elapsed_sec": int(_time.time() - start_clock),
                    }
                last_status = (f"row at {row_dt.strftime('%d/%m/%Y %H:%M')} "
                               "still running")
                break    # found recent row, keep polling
            else:
                last_status = f"no row matching {row_label!r} after submit"
        except Exception as e:
            last_status = f"poll error: {e}"
        if progress_cb is not None:
            try:
                progress_cb(int(_time.time() - start_clock), attempts, last_status)
            except Exception:
                pass
        _time.sleep(poll_interval)

    log.warning(f"WS poll — {label_for_log or row_label!r} TIMED OUT "
                f"after {attempts} attempt(s)")
    return {
        "ok":            False,
        "completed":     False,
        "timed_out":     True,
        "last_observed": last_status,
        "attempts":      attempts,
        "elapsed_sec":   int(_time.time() - start_clock),
    }


def ws_run_asset_perf_calc(daily_from: str, *,
                           daily_to: str | None = None,
                           session=None, base=None,
                           auth_cache_path: str = '',
                           wait_for_completion: bool = True,
                           poll_timeout_sec: int = 600,
                           progress_cb=None) -> dict:
    """Submit the Asset Performance Calculation form.

    `daily_from` is the previous working day in dd/MM/yyyy format;
    `daily_to` defaults to the current as-of date (which the form
    pre-fills from the server's session). Form discovery saves the
    other fields verbatim from the rendered page so the POST mirrors
    the operator's click exactly.

    When ``wait_for_completion=True`` (default), this function polls
    perfQueueDisplay.do until the queued row is marked Completed.
    Set False to return immediately after the queue-add response.
    """
    from datetime import datetime as _dt
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)

    r0 = session.get(f"{base}/{ASSET_PERF_FORM_URL}", timeout=30)
    r0.raise_for_status()
    soup = BeautifulSoup(r0.text, "html.parser")
    csrf = _csrf(soup)
    body: dict = {}
    for inp in soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name")
        if name:
            body[name] = inp.get("value", "") or ""
    # Visible inputs — pick up the From/To pairs and their defaults.
    def _get_val(name: str, default: str = "") -> str:
        inp = soup.find("input", {"name": name})
        return (inp.get("value") if inp else default) or default
    body["accid"]      = _get_val("accid", "0")
    body["fdate1"]     = _get_val("fdate1")    # monthly From
    body["todate1"]    = _get_val("todate1")   # monthly To
    body["fdate2"]     = daily_from            # ← what we override
    body["todate2"]    = daily_to or _get_val("todate2")
    # Selects
    for sel in soup.find_all("select"):
        name = sel.get("name") or ""
        if name in {"menuSearchPickList"}:
            continue
        chosen = sel.find("option", selected=True)
        body[name] = (chosen.get("value") if chosen else "") or ""
    body["WS_CSRFTOKEN"] = csrf
    body["submit"] = "Submit"

    submit_dt = _ist_naive_now()
    log.info(f"WS asset perf calc — daily_from={daily_from} "
             f"daily_to={body['todate2']}")
    r1 = session.post(f"{base}/{ASSET_PERF_POST_URL}", data=body,
                      headers={"Referer": f"{base}/{ASSET_PERF_FORM_URL}"},
                      timeout=180)
    r1.raise_for_status()
    text = r1.text
    log.info(f"WS asset perf calc — submitted HTTP {r1.status_code}")

    # Don't bother parsing the response body for "queued" — the form's
    # JS submits to /reportAssetPerfCalc.do AND redirects the page to
    # /perfQueueDisplay.do via retrieveURLForExeNWait. The kick-off
    # endpoint's response is never operator-facing, so its body shape
    # is undefined. The poll below is the source of truth: if we
    # observe a row whose Start time is at or after submit_dt with
    # the success icon, the job ran; otherwise it didn't.
    poll: dict = {}
    if wait_for_completion:
        poll = _ws_poll_for_completion(
            session, base,
            queue_url=ASSET_PERF_QUEUE_URL,
            row_label="Asset Performance Calculation",
            after_dt=submit_dt,
            timeout_sec=poll_timeout_sec,
            label_for_log="Asset Performance Calculation",
            progress_cb=progress_cb,
        )

    return {
        "ok":           bool(poll.get("completed")) if wait_for_completion
                        else (r1.status_code < 400),
        "completed":    poll.get("completed") if wait_for_completion else None,
        "timed_out":    poll.get("timed_out") if wait_for_completion else False,
        "poll_attempts": poll.get("attempts"),
        "poll_elapsed_sec": poll.get("elapsed_sec"),
        "last_observed": poll.get("last_observed", ''),
        "http_status":  r1.status_code,
        "response_text": text,
        "session":      session,
        "base":         base,
    }


BOD_ROW_LABELS = {
    "cash":                  "BOD Process - Cash & Equivalent",
    "saleable":              "BOD Process - Saleable holding",
    "order_exec_carry":      "BOD Process - Order execution reconciliation",
    "order_exec_reversal":   "BOD Process - Order execution reconciliation reversal",
    "carry_forward_reversal":"BOD Process - Carry forward reversal",
}


def ws_run_bod_process(process_kind: str, *,
                       as_on_date: str | None = None,
                       session=None, base=None,
                       auth_cache_path: str = '',
                       wait_for_completion: bool = True,
                       poll_timeout_sec: int = 600,
                       progress_cb=None) -> dict:
    """Submit the Begin of Day batch-job form for one process kind.

    `process_kind` is the friendly name: ``"cash"`` for Cash & Eq. or
    ``"saleable"`` for Saleable holding. The dropdown's actual posted
    value is looked up in BOD_PROCESS_VALUES.

    `as_on_date` is dd/MM/yyyy; defaults to whatever the form
    pre-fills (the server's session date).

    When ``wait_for_completion=True`` (default), this function polls
    the genericWait page until the queued row is marked Completed.
    """
    from datetime import datetime as _dt
    kind = (process_kind or "").strip().lower()
    if kind not in BOD_PROCESS_VALUES:
        raise ValueError(f"Unknown BOD process kind {process_kind!r}; "
                         f"expected one of {list(BOD_PROCESS_VALUES)}")
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)

    r0 = session.get(f"{base}/{BOD_BATCH_FORM_URL}", timeout=30)
    r0.raise_for_status()
    soup = BeautifulSoup(r0.text, "html.parser")
    csrf = _csrf(soup)
    body: dict = {}
    for inp in soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name")
        if name:
            body[name] = inp.get("value", "") or ""
    def _get_val(name: str, default: str = "") -> str:
        inp = soup.find("input", {"name": name})
        return (inp.get("value") if inp else default) or default
    body["accountid"] = _get_val("accountid", "0")
    body["asondate"]  = as_on_date or _get_val("asondate")
    body["symbolid"]  = _get_val("symbolid", "")
    for sel in soup.find_all("select"):
        name = sel.get("name") or ""
        if name in {"menuSearchPickList"}:
            continue
        chosen = sel.find("option", selected=True)
        body[name] = (chosen.get("value") if chosen else "") or ""
        # Log every option in the Process dropdown so the operator can
        # verify our BOD_PROCESS_VALUES mapping against the live form.
        # Used the first time order_exec_* kinds run to confirm codes.
        if name == "option":
            opts = [(o.get("value", ""), (o.get_text() or "").strip())
                    for o in sel.find_all("option")]
            log.info(f"WS batch — process dropdown options: {opts}")
    body["option"]       = BOD_PROCESS_VALUES[kind]
    body["process"]      = "P"   # finalcheck('P') → process mode
    body["WS_CSRFTOKEN"] = csrf
    body["sub"]          = "Process"

    submit_dt = _ist_naive_now()
    log.info(f"WS batch — kind={kind} option={body['option']} "
             f"as_on={body['asondate']}")
    r1 = session.post(f"{base}/{BOD_BATCH_POST_URL}", data=body,
                      headers={"Referer": f"{base}/{BOD_BATCH_FORM_URL}"},
                      timeout=300)
    r1.raise_for_status()
    text = r1.text
    log.info(f"WS batch — submitted HTTP {r1.status_code}")

    # Same reasoning as ws_run_asset_perf_calc: the kick-off response
    # is undefined; success is determined by the poll on genericWait.
    poll: dict = {}
    if wait_for_completion:
        poll = _ws_poll_for_completion(
            session, base,
            queue_url=PRICE_WAIT_URL,
            row_label=BOD_ROW_LABELS[kind],
            after_dt=submit_dt,
            timeout_sec=poll_timeout_sec,
            label_for_log=BOD_ROW_LABELS[kind],
            progress_cb=progress_cb,
        )

    return {
        "ok":            bool(poll.get("completed")) if wait_for_completion
                         else (r1.status_code < 400),
        "completed":     poll.get("completed") if wait_for_completion else None,
        "timed_out":     poll.get("timed_out") if wait_for_completion else False,
        "poll_attempts": poll.get("attempts"),
        "poll_elapsed_sec": poll.get("elapsed_sec"),
        "last_observed": poll.get("last_observed", ''),
        "http_status":   r1.status_code,
        "response_text": text,
        "session":       session,
        "base":          base,
    }


# ============================================================================ #
# EoD Pipeline — Vidal corporate action upload, Batch Corporate Action,
# Price Verification, Transaction Reconciliation report.
# ============================================================================ #
#
# Mirrors the operator-clicked EoD process per the Evening EOD Process video.
# Each bridge is a small, idempotent step that reads a form, posts the
# operator-equivalent body, and polls the genericWait/exeWait page for
# the row's Completed icon (same convention as the morning BOD bridges).
# ============================================================================ #

CORP_ACTION_UPLOAD_FORM_URL = (
    "redirect.do?target=vidalCorpActionUpload&scope=*"
    "&cmScope=*&consolidation=C&srcMenuId=2079")
# Both multipart upload and the mode=process kick-off POST to this
# URL (verified against operator's captured payload). Hardcoded
# rather than scraped from the form action because the scrape
# returns "/fincrm/vidalCorpActionUpload.do" which, after the
# leading-slash strip and join with base="…/fincrm", produces a
# duplicate "/fincrm/fincrm/" path. Tomcat tolerates it, but cleaner
# to use a known relative path.
CORP_ACTION_POST_URL = "vidalCorpActionUpload.do"

BATCH_CORP_ACTION_FORM_URL = (
    "redirect.do?target=batchCorpAction&scope=*&cmScope=*"
    "&consolidation=C&srcMenuId=192")
BATCH_CORP_ACTION_POST_URL = "batchCorpAction.do"
BATCH_CORP_ROW_LABEL       = "Batch Corporate Action"

# Apply Corp Actions — runs as the FINCRM_USER2 account (separate from
# the upload user; WS forbids same-user upload+apply). Two-step protocol:
#   1. POST mode=modify + headerdate + sub/save=Query → returns the
#      list of pending corp actions for that asondate. Hidden inputs
#      give us applied[i] / corpactionid[i] / symbolcode1[i] for each row.
#   2. POST mode=update + allselect=on + cancelSel[i]=on for each row →
#      finalises the apply.
APPLY_CORP_FORM_URL = "applyCorp.do"
APPLY_CORP_POST_URL = "applyCorp.do"

PRICE_VERIFICATION_URL     = "reportPriceVerification.do"

RECON_STMT_FORM_URL        = (
    "redirect.do?target=reportReconciliationStatement&scope=*&cmScope=*"
    "&consolidation=C&srcMenuId=1544")
RECON_STMT_POST_URL        = "reportReconcileHolding.do"
RECON_STMT_ROW_LABEL       = "Transaction Reconciliation"
RECON_EXE_WAIT_URL         = "genericExeWait.do"


def _scrape_form_action(html: str, default: str = "") -> str:
    """Return the first <form>'s action attribute, or `default`."""
    soup = BeautifulSoup(html, "html.parser")
    f = soup.find("form")
    if not f:
        return default
    return (f.get("action") or default).lstrip("/")


def ws_upload_corp_action_file(file_path: str | Path, *,
                                mapid: int = 1028,
                                source: str = "vidal_corp_map_new",
                                date_format: str = "dd/MM/yyyy",
                                session=None, base=None,
                                auth_cache_path: str = '',
                                wait_for_completion: bool = True,
                                poll_timeout_sec: int = 600,
                                progress_cb=None,
                                debug_dump_dir: str | Path | None = None) -> dict:
    """Upload a Vidal corporate-action CSV to WS Map ID 1028.

    Replicates the operator clicks the video shows for the
    vidalCorpActionUpload page (srcMenuId=2079):

      1. GET the form, scrape hidden inputs + the form's action URL.
      2. Multipart POST the file with mapid/source/sname/format set.
      3a. POST to queryPricePosting.do with mode=checkDuplicateFile.
          WS returns an actionString:
            - ``next`` — no duplicate, proceed normally
            - ``nextWithOKContinue`` — file already exists; the
              browser pops "This File is already uploaded.  Do you
              want to proceed?" with OK/Cancel; OK → override
            - ``nextWithCancel`` — duplicates blocked, hard fail
      3b. POST with mode=errorPage and the actionString echoed back.
      3c. Form-submit to genericWait.jsp — required to actually queue
          the job, mirrors the browser's setExecWaitAction() form
          submit. (See the WS upload 3-step pattern memo.)

    On submit, WS queues a row labelled "Vidal Corporate Action Upload"
    in genericWait.jsp; the bridge polls for that row's Completed icon
    when ``wait_for_completion=True``.

    Returns ``{ok, completed, timed_out, duplicate, filename, mapid,
    http_status, session, base, error_message}``. ``duplicate=True``
    indicates an override happened — operator-relevant for audit.
    """
    from datetime import datetime as _dt
    import re as _re
    fp = Path(file_path)
    if not fp.exists():
        return {"ok": False, "error": f"file not found: {fp}",
                "session": session, "base": base}

    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)

    _dump_dir = Path(debug_dump_dir) if debug_dump_dir else \
        Path(os.environ.get("KEYSTONE_DATA_DIR", ".")) / "data" / "ws_corp_probe"
    _dump_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: GET form, scrape state. ──────────────────────────────
    if progress_cb:
        progress_cb("preparing", "Loading Vidal corp action form…")
    r0 = session.get(f"{base}/{CORP_ACTION_UPLOAD_FORM_URL}", timeout=30)
    r0.raise_for_status()
    (_dump_dir / "corp_step0_form.html").write_text(
        r0.text, encoding="utf-8", errors="replace")
    fields0 = _scrape_all_inputs(r0.text)
    asondate0 = fields0.get("asondate", "")
    log.info(f"WS corp upload — initial form fields={list(fields0)[:8]} "
             f"asondate={asondate0!r}")

    # ── Step 2: multipart POST. ──────────────────────────────────────
    # Strip the email_ingestor's `_YYYYMMDDTHHMMSS` filename stamp
    # before sending to WS — the corp action mapper looks for the
    # bare `XC{ddmmyy}.csv` pattern.
    bare_name = _re.sub(r'_\d{8}T\d{6}(?=\.[^.]+$)', '', fp.name)
    if bare_name != fp.name:
        log.info(f"WS corp upload — using bare filename {bare_name!r} "
                 f"(stripped timestamp suffix from {fp.name!r})")

    # Form-derived field names (per the live page's HTML, captured by
    # the operator):
    #   <form action="/fincrm/vidalCorpActionUpload.do" enctype="multipart/form-data">
    #     <input type="hidden" name="mode" value="regular">  ← JS flips to "upload" before submit
    #     <input type="file"   name="fileName">              ← NOT "fname"
    #     <input type="hidden" name="format" value="dd/MM/yyyy">
    #     <input type="hidden" name="source" />              ← set by PickList JS
    #     <input type="hidden" name="srcMenuId" value="2079">
    #     <input type="text"   name="asondate" value="...">
    #     <input type="text"   name="mapid" value="0" disabled>  ← set by PickList JS
    #     <input type="hidden" name="WS_CSRFTOKEN" value="...">
    #   </form>
    #
    # The form's setExecWaitAction() runs onsubmit and sets:
    #   document.forms[0].mode.value = "upload"
    # before letting the form submit, so the actual multipart POST
    # carries mode="upload" — NOT "regular". That's the canonical
    # signal to the action class to run the upload pathway.
    fields0["mode"]   = "upload"
    fields0["mapid"]  = str(mapid)
    fields0["source"] = source
    fields0["format"] = date_format
    # The clicked submit button's name=value pair. Browser sends it
    # (Struts actions occasionally branch on it). _scrape_all_inputs
    # skips type=submit, so add it explicitly.
    fields0["submit"] = "Upload"

    files = {
        "fileName": (bare_name, fp.read_bytes(),
                     "text/csv" if fp.suffix.lower() == ".csv"
                     else "application/octet-stream"),
    }

    submit_dt = _ist_naive_now()
    if progress_cb:
        progress_cb("uploading", f"Uploading {bare_name}…")
    log.info(f"WS corp upload — POST {CORP_ACTION_POST_URL} mode=upload "
             f"mapid={mapid} source={source} fileName={bare_name} "
             f"({fp.stat().st_size}B)")
    # Build full Referer + Origin URLs explicitly so they look identical
    # to a browser-driven submit. Some CSRF middleware checks Origin
    # against the application's host.
    from urllib.parse import urlsplit
    _origin = f"{urlsplit(base).scheme}://{urlsplit(base).netloc}"
    r1 = session.post(f"{base}/{CORP_ACTION_POST_URL}",
                      data=fields0, files=files,
                      headers={
                          "Referer": f"{base}/{CORP_ACTION_UPLOAD_FORM_URL}",
                          "Origin":  _origin,
                      },
                      timeout=300)
    r1.raise_for_status()
    (_dump_dir / "corp_step2_response.html").write_text(
        r1.text, encoding="utf-8", errors="replace")
    log.info(f"WS corp upload — multipart POST HTTP {r1.status_code}")

    # Scrape ALL inputs from r1 response — this carries the per-upload
    # state we need to echo into the kick-off (server tempfile + fresh
    # CSRF + asondate). Corp action's protocol DIFFERS from price
    # upload's: per the operator-captured form payload, the kick-off
    # body uses
    #
    #   WS_CSRFTOKEN <new>
    #   mapidstr     1028
    #   oldNAV       null
    #   asondate     dd/MM/yyyy
    #   tempfile     <FILENAME>
    #   mode         process
    #
    # No `mode=checkDuplicateFile` XHR, no `actionString`, no `source`,
    # no `format`. The submit URL is the same form action
    # (vidalCorpActionUpload.do) the multipart POST hit.
    fields2 = _scrape_all_inputs(r1.text)
    # Use bare_name as the fallback — the server's tempfile, if present,
    # already reflects the name we sent (bare). Falling back to fp.name
    # would re-introduce the timestamped form and the mapper exception.
    server_tempfile = fields2.get("tempfile") or bare_name
    new_csrf  = fields2.get("WS_CSRFTOKEN", "") or fields0.get("WS_CSRFTOKEN", "")
    # asondate fallback chain: r1 response → r0 initial form → today
    # (in dd/MM/yyyy as the form expects). Empty asondate makes WS
    # reject mode=process with a generic page.
    asondate = (fields2.get("asondate")
                or asondate0
                or _ist_naive_now().strftime("%d/%m/%Y"))
    log.info(f"WS corp upload — server tempfile={server_tempfile!r} "
             f"asondate={asondate!r}")

    body_kickoff = {
        "WS_CSRFTOKEN": new_csrf,
        "mapidstr":     str(mapid),
        "oldNAV":       "null",
        "asondate":     asondate,
        "tempfile":     server_tempfile,
        "mode":         "process",
    }

    # ── Step 3: kick off (mode=process). ─────────────────────────────
    if progress_cb:
        progress_cb("processing", "Processing corp action file…")
    log.info(f"WS corp upload — kick-off mode=process tempfile={server_tempfile!r} "
             f"asondate={asondate!r}")
    r3 = session.post(f"{base}/{CORP_ACTION_POST_URL}",
                      data=body_kickoff,
                      headers={"Referer": f"{base}/{CORP_ACTION_POST_URL}"},
                      timeout=300)
    r3.raise_for_status()
    (_dump_dir / "corp_step3_process.html").write_text(
        r3.text, encoding="utf-8", errors="replace")
    log.info(f"WS corp upload — kick-off HTTP {r3.status_code}")

    # ── Step 4: form submit to genericWait.jsp so the row appears in
    # the operator's queue. Best-effort — corp action processes
    # synchronously in step 3, this just navigates to the wait page.
    try:
        r4 = session.post(f"{base}/{PRICE_WAIT_URL}",
                          data=body_kickoff,
                          headers={"Referer": f"{base}/{CORP_ACTION_POST_URL}"},
                          timeout=60)
        (_dump_dir / "corp_step4_wait.html").write_text(
            r4.text, encoding="utf-8", errors="replace")
        log.info(f"WS corp upload — wait-page submit HTTP {r4.status_code}")
    except Exception as _e:
        log.warning(f"WS corp upload — wait-page submit failed: {_e}")

    # ── Step 5: poll for the "Vidal Corporate Action Upload" row. ────
    poll: dict = {}
    if wait_for_completion:
        poll = _ws_poll_for_completion(
            session, base,
            queue_url=PRICE_WAIT_URL,
            row_label="Vidal Corporate Action Upload",
            after_dt=submit_dt,
            timeout_sec=poll_timeout_sec,
            label_for_log="Vidal Corporate Action Upload",
            progress_cb=progress_cb,
        )

    log.info(f"WS corp upload — poll_completed={poll.get('completed')} "
             f"poll_timed_out={poll.get('timed_out')}")

    return {
        "ok":            bool(poll.get("completed")) if wait_for_completion
                          else (r3.status_code < 400),
        "completed":     poll.get("completed") if wait_for_completion else None,
        "timed_out":     poll.get("timed_out") if wait_for_completion else False,
        # Corp action upload doesn't expose duplicate-detection through
        # the actionString protocol; treat all as fresh. If WS is
        # configured to refuse duplicates the kick-off response will
        # surface that directly (and we'd parse it later if needed).
        "duplicate":     False,
        "filename":      fp.name,
        "mapid":         mapid,
        "http_status":   r3.status_code,
        "session":       session,
        "base":          base,
    }


def ws_apply_corp_actions(as_on_date: str, *,
                           auth_cache_path: str = '',
                           progress_cb=None) -> dict:
    """Apply pending corporate actions for `as_on_date` (DD/MM/YYYY).

    Mirrors the operator's manual flow on /fincrm/applyCorp.do:
      1. POST mode=modify + headerdate=DD/MM/YYYY + save=Query → server
         renders the list of corp actions for that date.
      2. Parse rows from the response (applied[i], corpactionid[i],
         symbolcode1[i]).
      3. POST mode=update + allselect=on + cancelSel[i]=on for every
         detected row → server commits the apply.

    Always opens a fresh session as the FINCRM_USER2 account; the
    portal forbids same-user upload+apply, so this MUST be a different
    user from the rest of the EoD pipeline. Skips the step (returns
    ok=True, skipped=True) when apply creds are not configured so the
    operator's manual workflow keeps working until creds are set.
    """
    import re as _re

    if not apply_creds_configured():
        log.info("Apply Corp Actions skipped — FINCRM_USER2 / FINCRM_PASS2 "
                 "not configured")
        return {
            "ok":      True,
            "skipped": True,
            "reason":  "apply credentials not configured",
        }

    session, base = _pc_open_apply_session(auth_cache_path)
    if progress_cb:
        try: progress_cb("login", "Logged in as apply user")
        except Exception: pass

    # Step 1 — open the form, scrape CSRF + base hidden fields
    r0 = session.get(f"{base}/{APPLY_CORP_FORM_URL}", timeout=30)
    r0.raise_for_status()
    soup0 = BeautifulSoup(r0.text, "html.parser")
    csrf0 = _csrf(soup0)

    # Step 2 — POST mode=modify with headerdate to fetch the row list.
    body_query = {
        "format":         "dd/MM/yyyy",
        "mode":           "modify",
        "headerdate":     as_on_date,
        "corptypeFilter": "",
        "statusFilter":   "",
        "save":           "Query",
        "WS_CSRFTOKEN":   csrf0,
    }
    if progress_cb:
        try: progress_cb("query", f"Querying corp actions for {as_on_date}")
        except Exception: pass
    log.info(f"WS Apply Corp Actions — querying for {as_on_date}")
    r1 = session.post(f"{base}/{APPLY_CORP_POST_URL}", data=body_query,
                      headers={"Referer": f"{base}/{APPLY_CORP_FORM_URL}"},
                      timeout=120)
    r1.raise_for_status()

    # Parse response: each row has hidden inputs applied[i], corpactionid[i],
    # symbolcode1[i] (the applied[] state shows current Y/N value). Pull all
    # three by index and pair them up. CSRF is rotated on the response.
    soup1 = BeautifulSoup(r1.text, "html.parser")
    csrf1 = _csrf(soup1) or csrf0
    rows: dict[int, dict] = {}
    rx = _re.compile(r'^(applied|corpactionid|symbolcode1)\[(\d+)\]$')
    for inp in soup1.find_all("input"):
        name = inp.get("name") or ""
        m = rx.match(name)
        if not m:
            continue
        field, idx = m.group(1), int(m.group(2))
        rows.setdefault(idx, {})[field] = inp.get("value", "") or ""

    # Sort by index to keep the original WS row order
    row_indices = sorted(rows.keys())
    if not row_indices:
        log.info("WS Apply Corp Actions — no corp action rows for this date")
        return {
            "ok":            True,
            "applied":       0,
            "by_corptype":   {},
            "session":       session,
            "base":          base,
        }

    # Build the update payload — apply ALL detected rows.
    body_save: dict = {
        "format":         "dd/MM/yyyy",
        "mode":           "update",
        "headerdate":     as_on_date,
        "corptypeFilter": "",
        "statusFilter":   "",
        "allselect":      "on",
        "WS_CSRFTOKEN":   csrf1,
    }
    by_corptype: dict[str, int] = {}
    # requests serialises repeated keys when value is a list of tuples,
    # but we have indexed names (applied[0], applied[1], ...) — so flat
    # dict is fine.
    for idx in row_indices:
        r = rows[idx]
        body_save[f"applied[{idx}]"]      = "Y"
        body_save[f"cancelSel[{idx}]"]    = "on"
        body_save[f"corpactionid[{idx}]"] = r.get("corpactionid", "")
        body_save[f"symbolcode1[{idx}]"]  = r.get("symbolcode1", "")
        ctype = r.get("corpactionid", "?")
        by_corptype[ctype] = by_corptype.get(ctype, 0) + 1

    if progress_cb:
        try:
            progress_cb("save", f"Applying {len(row_indices)} corp action(s)")
        except Exception:
            pass
    log.info(f"WS Apply Corp Actions — saving {len(row_indices)} row(s) "
             f"({dict(by_corptype)})")
    r2 = session.post(f"{base}/{APPLY_CORP_POST_URL}", data=body_save,
                      headers={"Referer": f"{base}/{APPLY_CORP_POST_URL}"},
                      timeout=120)
    r2.raise_for_status()

    # WS does not give a structured success response on this form — the
    # response is the same applyCorp.do page with the rows now showing
    # applied=Y. The reliable signal is the post-state: count how many
    # rows now have applied=Y and require it covers the rows we toggled.
    # An earlier version did string-matching on words like "error" and
    # "required", which appear in the WS template asset names + label
    # text on every page (success or failure), producing false ok=False
    # even when applied=N matched confirmed_Y_after.
    def _count_confirmed_y(html: str) -> int:
        s = BeautifulSoup(html, "html.parser")
        return sum(
            1 for inp in s.find_all("input")
            if rx.match(inp.get("name") or "")
            and rx.match(inp.get("name") or "").group(1) == "applied"
            and (inp.get("value") or "").upper() == "Y"
        )

    confirmed_y = _count_confirmed_y(r2.text)

    # WS sometimes lags on the applied=Y flag — the row HAS been
    # committed but the form re-render races the DB write, so the
    # first read sees 12/13 instead of 13/13. Re-fetch the form once
    # after a short pause to absorb that race; only flag a real
    # short-fall after the second read.
    has_hard_error = bool(_detect_ws_hard_error(r2.text or ''))
    if (not has_hard_error
            and r2.status_code < 400
            and confirmed_y < len(row_indices)):
        import time as _time
        _time.sleep(0.5)
        log.info(f"WS Apply Corp Actions — re-reading "
                 f"(first pass: confirmed_Y={confirmed_y}/{len(row_indices)})")
        # Re-scrape CSRF from r2 (the apply response) — WS rotates
        # WS_CSRFTOKEN on every form-page response, so reusing csrf0
        # (from r0, two responses ago) can be silently rejected. Build
        # the re-read body around the rotated token.
        _retry_csrf = _csrf(BeautifulSoup(r2.text, "html.parser")) or csrf1
        _retry_body = dict(body_query)
        _retry_body["WS_CSRFTOKEN"] = _retry_csrf
        r3 = session.post(f"{base}/{APPLY_CORP_POST_URL}", data=_retry_body,
                          headers={"Referer": f"{base}/{APPLY_CORP_POST_URL}"},
                          timeout=60)
        if r3.status_code < 400 and not _detect_ws_hard_error(r3.text or ''):
            confirmed_y = max(confirmed_y, _count_confirmed_y(r3.text))

    # ok now treats any-rows-confirmed as success: the EoD pipeline
    # downstream of Apply Corp processes whatever rows are actually
    # marked Y, so a partial commit (e.g. 12/13 — one row legitimately
    # rejected by WS) shouldn't fail the whole step. Hard errors and
    # zero-rows-confirmed still flip ok to False.
    ok = (
        r2.status_code < 400
        and not has_hard_error
        and confirmed_y > 0
    )
    partial = ok and confirmed_y < len(row_indices)

    log.info(f"WS Apply Corp Actions — done: ok={ok} "
             f"applied={len(row_indices)} confirmed_Y_after={confirmed_y}"
             + (" (partial)" if partial else ""))

    return {
        "ok":            ok,
        "applied":       len(row_indices),
        "confirmed_y":   confirmed_y,
        "partial":       partial,
        "by_corptype":   dict(sorted(by_corptype.items())),
        "http_status":   r2.status_code,
        "session":       session,
        "base":          base,
    }


def ws_run_batch_corp_action(as_on_date: str | None = None, *,
                              session=None, base=None,
                              auth_cache_path: str = '',
                              wait_for_completion: bool = True,
                              poll_timeout_sec: int = 600,
                              progress_cb=None) -> dict:
    """Submit the Batch Corporate Action job. Mirrors the operator's
    "click Apply on the batchCorpAction page" — visits the form, scrapes
    hidden inputs + selects, posts with sub=Apply, then polls
    genericWait for the "Batch Corporate Action" row's success icon.
    """
    from datetime import datetime as _dt
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)

    r0 = session.get(f"{base}/{BATCH_CORP_ACTION_FORM_URL}", timeout=30)
    r0.raise_for_status()
    soup = BeautifulSoup(r0.text, "html.parser")
    csrf = _csrf(soup)
    body: dict = {}
    # Scrape ALL input fields, not just type=hidden. The BCA form's
    # date-picker inputs (fromdate, todate, asondate) are type=text or
    # type=date and would otherwise be missed — leaving them null in
    # the POST and tripping the Java handler's alreadydone fall-through.
    # Skip non-data input types so we don't drag submit/button labels
    # into the body.
    _SKIP_INPUT_TYPES = {"submit", "button", "reset", "image", "file"}
    for inp in soup.find_all("input"):
        name = inp.get("name")
        typ  = (inp.get("type") or "text").lower()
        if not name or typ in _SKIP_INPUT_TYPES:
            continue
        body[name] = inp.get("value", "") or ""
    def _get_val(name: str, default: str = "") -> str:
        inp = soup.find("input", {"name": name})
        return (inp.get("value") if inp else default) or default
    if as_on_date:
        body["asondate"] = as_on_date
    else:
        body.setdefault("asondate", _get_val("asondate"))
    # WS BCA expects an explicit fromdate/todate range — both equal
    # the as_on_date. Belt-and-braces: even though the all-inputs
    # scrape above usually picks up the form's pre-populated values,
    # explicitly set them here so they're never null in the POST. The
    # Java handler bails through the alreadydone forward
    # (BatchCorpAction.java:271) without processing anything when the
    # date range comes through as null, even though real work is
    # pending — this is the malformed-request fall-through path.
    _bca_date = body.get("asondate") or as_on_date or ""
    if _bca_date:
        body["fromdate"] = _bca_date
        body["todate"]   = _bca_date
    for sel in soup.find_all("select"):
        name = sel.get("name") or ""
        if name in {"menuSearchPickList"}:
            continue
        chosen = sel.find("option", selected=True)
        body[name] = (chosen.get("value") if chosen else "") or ""
    body["WS_CSRFTOKEN"] = csrf
    body["sub"]          = "Apply"

    submit_dt = _ist_naive_now()
    log.info(f"WS Batch Corp Action — as_on={body.get('asondate', '?')}")
    r1 = session.post(f"{base}/{BATCH_CORP_ACTION_POST_URL}", data=body,
                      headers={"Referer": f"{base}/{BATCH_CORP_ACTION_FORM_URL}"},
                      timeout=300)
    # Hard-error scan AFTER raise_for_status — WS returns 200 with login
    # form re-render or stack trace on session expiry / validation
    # failure. Detecting those here saves a 600s poll-timeout at the
    # next step.
    _assert_ws_response_ok(r1, context='Batch Corp Action submit')
    log.info(f"WS Batch Corp Action — submitted HTTP {r1.status_code}")

    # WS idempotency: BatchCorpAction.perform forwards to alreadydone.jsp
    # when the batch has genuinely already been run for the asondate
    # (rare — typically only if the operator manually ran it and then
    # the EoD pipeline picks up the same date). The body of the
    # forwarded page contains "already processed" / "alreadydone".
    #
    # NOTE: alreadydone is ALSO the Java handler's fall-through when
    # required form fields are missing (e.g. fromdate/todate null).
    # That used to mask real bugs as "success". Now that we explicitly
    # post fromdate/todate above, the only path that lands here is the
    # legitimate "really done" case — keeping the body-text check as
    # a defensive shortcut for that.
    body_lc = (r1.text or '').lower()
    if 'alreadydone' in body_lc or 'already done' in body_lc:
        log.info("WS Batch Corp Action — body indicates already done for "
                 "this date; treating as success")
        return {
            "ok":            True,
            "completed":     True,
            "already_done":  True,
            "timed_out":     False,
            "last_observed": "already_done — corp actions for this date "
                              "had already been processed",
            "http_status":   r1.status_code,
            "session":       session,
            "base":          base,
        }
    # Diagnostic: capture URL + redirect history + body length so the
    # next incident is faster to triage without re-running the bridge.
    if r1.status_code < 400:
        _hist_summary = [(h.status_code, h.url) for h in (r1.history or [])]
        log.info(f"WS Batch Corp Action — submitted, awaiting queue. "
                 f"final_url={r1.url!r} history={_hist_summary} "
                 f"body_len={len(r1.text or '')}")

    poll: dict = {}
    if wait_for_completion:
        poll = _ws_poll_for_completion(
            session, base,
            queue_url=PRICE_WAIT_URL,
            row_label=BATCH_CORP_ROW_LABEL,
            after_dt=submit_dt,
            timeout_sec=poll_timeout_sec,
            label_for_log=BATCH_CORP_ROW_LABEL,
            progress_cb=progress_cb,
        )

    return {
        "ok":            bool(poll.get("completed")) if wait_for_completion
                          else (r1.status_code < 400),
        "completed":     poll.get("completed") if wait_for_completion else None,
        "already_done":  False,
        "timed_out":     poll.get("timed_out") if wait_for_completion else False,
        "poll_attempts": poll.get("attempts"),
        "poll_elapsed_sec": poll.get("elapsed_sec"),
        "last_observed": poll.get("last_observed", ''),
        "http_status":   r1.status_code,
        "session":       session,
        "base":          base,
    }


def ws_run_price_verification(as_on_date: str | None = None, *,
                               option: str = "ALL",
                               session=None, base=None,
                               auth_cache_path: str = '',
                               progress_cb=None) -> dict:
    """Run the Price Verification report with Option=Both (Missing + Stale).

    Returns the parsed missing/stale list as ``{missing: [...], stale: [...]}``
    plus the raw HTML for fallback inspection. Does NOT poll genericWait —
    Price Verification renders inline on the same page, no queue.
    """
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)

    r0 = session.get(f"{base}/{PRICE_VERIFICATION_URL}", timeout=30)
    r0.raise_for_status()
    soup = BeautifulSoup(r0.text, "html.parser")
    csrf = _csrf(soup)
    body: dict = {}
    for inp in soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name")
        if name:
            body[name] = inp.get("value", "") or ""
    def _get_val(name: str, default: str = "") -> str:
        inp = soup.find("input", {"name": name})
        return (inp.get("value") if inp else default) or default
    body["queryDate"]      = as_on_date or _get_val("queryDate")
    body["stalePriceDays"] = _get_val("stalePriceDays", "10")
    body["securityType"]   = _get_val("securityType", "ALL")
    body["selectedoption"] = option   # MP | SP | ALL  (ALL = Both)
    body["priceSource"]    = _get_val("priceSource", "CloseIndex")
    body["outputFormat"]   = _get_val("outputFormat", "html")
    body["WS_CSRFTOKEN"]   = csrf
    body["sub"]            = "Submit"

    log.info(f"WS Price Verification — option={option} as_on={body.get('queryDate')}")
    r1 = session.post(f"{base}/{PRICE_VERIFICATION_URL}", data=body,
                      headers={"Referer": f"{base}/{PRICE_VERIFICATION_URL}"},
                      timeout=120)
    r1.raise_for_status()

    soup1 = BeautifulSoup(r1.text, "html.parser")
    def _scrape_block(heading_hint: str) -> list[dict]:
        # The page renders results as adjacent tables under an <h*> or
        # <td class="…ColHead…">heading. Find a row whose first cell
        # text contains the heading hint, then collect rows until next
        # heading row appears.
        out: list[dict] = []
        rows = soup1.find_all("tr")
        capture = False
        headers: list[str] = []
        for tr in rows:
            cells = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
            if not cells:
                continue
            joined = " ".join(cells).lower()
            if heading_hint.lower() in joined and len(cells) <= 2:
                capture = True
                headers = []
                continue
            if not capture:
                continue
            # Header row (Security Code / Security Name / Price)
            if any(h in cells[0].lower() for h in ("security code",)) and not headers:
                headers = [c.lower().replace(" ", "_") for c in cells]
                continue
            # Stop on a follow-up section header
            if len(cells) <= 2 and cells[0] and not cells[0][0].isdigit():
                break
            if headers and len(cells) >= len(headers):
                out.append(dict(zip(headers, cells[:len(headers)])))
        return out

    return {
        "ok":      r1.status_code < 400,
        "missing": _scrape_block("Missing Prices"),
        "stale":   _scrape_block("Stale Prices"),
        "html":    r1.text,
        "session": session,
        "base":    base,
    }


def ws_run_recon_query(as_on_date: str | None = None, *,
                       session=None, base=None,
                       auth_cache_path: str = '',
                       wait_for_completion: bool = True,
                       poll_timeout_sec: int = 30,
                       output_dir: str | Path | None = None,
                       progress_cb=None) -> dict:
    """Run the Transaction Reconciliation report and download the PDF.

    Mirrors the operator click flow:

      1. GET ``reportReconciliationStatement?srcMenuId=1544``, POST Query.
      2. Lands on ``protected/withMenu.jsp?self=genericWait.jsp``.
         Initial row carries ``wait.gif``; once the report is generated
         the icon transitions to ``successlink.bmp`` (alt=Completed).
         Poll until that transition or timeout.
      3. The row's ``<a>`` element wraps the icon and carries an
         ``href="javascript:onClkLink('A','TransactionReconciliationProcessLog');"``
         shim. Per the page's inline JS, clicking it POSTs to
         ``genericExeWait.do`` with ``scopeType``, ``scopeAttr``,
         ``refreshContent=call`` and ``WS_CSRFTOKEN``.
      4. The genericExeWait.do response carries a "Show Report" anchor:
         ``<a href="javascript:reportcall('/fincrm/servlet/report?fn=Z..._ReconciliationStatement1544Z.pdf', …)">``.
         Extract the fn= URL (filename varies per run), GET the PDF, save.

    Returns ``{ok, pdf_path, pdf_url, …}``. Caller parses the PDF via
    ``core.recon_pdf_parser`` and feeds it into the EoD email.
    """
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)

    out_dir = Path(output_dir) if output_dir else \
        Path(os.environ.get("KEYSTONE_DATA_DIR", ".")) / "data" / "ws_recon"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Step 1 — load the form, scrape state, post Query.
    # Scrape every <input> (hidden, checkbox, text) and every <select>
    # so the POST mirrors what a real Query click sends — including the
    # 11 reconciliation-flag checkboxes (holdingReconcile,
    # cashReconciliation, draftTransactions, bankReconciliation,
    # performanceReconcile, rateCheck, faceValueExcep, suspendedClient,
    # suspendedSecurity, misMatchAlloc, invalidSecurity). Without those
    # checkboxes, WS receives the form with no recon flags set and
    # silently no-ops — no Transaction Reconciliation row queued.
    r0 = session.get(f"{base}/{RECON_STMT_FORM_URL}", timeout=30)
    r0.raise_for_status()
    soup = BeautifulSoup(r0.text, "html.parser")
    csrf = _csrf(soup)
    body: dict = {}
    _SKIP_INPUT_TYPES = {"submit", "reset", "button", "image", "file"}
    for inp in soup.find_all("input"):
        name = inp.get("name")
        typ  = (inp.get("type") or "text").lower()
        if not name or typ in _SKIP_INPUT_TYPES:
            continue
        if typ == "checkbox":
            # Send the checkbox value only if it would be checked when
            # the form first loads (defaultChecked / checked attribute).
            if inp.has_attr("checked"):
                body[name] = inp.get("value", "Y") or "Y"
            continue
        if typ == "radio":
            if inp.has_attr("checked"):
                body[name] = inp.get("value", "") or ""
            continue
        body[name] = inp.get("value", "") or ""
    if as_on_date:
        # Form's date field is `todate` (not `queryDate` despite the
        # menu label). Confirmed by the live form's network capture.
        body["todate"] = as_on_date
    for sel in soup.find_all("select"):
        name = sel.get("name") or ""
        if name in {"menuSearchPickList"}:
            continue
        chosen = sel.find("option", selected=True)
        body[name] = (chosen.get("value") if chosen else "") or ""
    body["WS_CSRFTOKEN"] = csrf
    body["submit"]       = "Query"

    submit_dt = _ist_naive_now()
    log.info(f"WS Recon Query — as_on={body.get('todate', '?')} "
             f"flags={sorted(k for k, v in body.items() if v == 'Y')}")
    r1 = session.post(f"{base}/{RECON_STMT_POST_URL}", data=body,
                      headers={"Referer": f"{base}/{RECON_STMT_FORM_URL}"},
                      timeout=120)
    _assert_ws_response_ok(r1, context='Recon Query submit')

    # Step 2 — poll the genericWait page until the row shows the
    # successlink.bmp / Completed icon (alt="Completed" in the IMG).
    poll: dict = {}
    if wait_for_completion:
        poll = _ws_poll_for_completion(
            session, base,
            queue_url=PRICE_WAIT_URL,
            row_label=RECON_STMT_ROW_LABEL,
            after_dt=submit_dt,
            timeout_sec=poll_timeout_sec,
            label_for_log=RECON_STMT_ROW_LABEL,
            progress_cb=progress_cb,
        )
    if not (poll.get("completed") if wait_for_completion else True):
        return {"ok": False, "completed": False,
                "timed_out": poll.get("timed_out", False),
                "last_observed": poll.get("last_observed", ''),
                "session": session, "base": base}

    # Step 3 — fetch the wait page once more, extract the row's
    # onClkLink scope, and POST genericExeWait.do.
    if progress_cb:
        progress_cb("locating", "Locating Show Report link…")
    rwait = session.get(f"{base}/{PRICE_WAIT_URL}", timeout=30)
    rwait.raise_for_status()
    swait = BeautifulSoup(rwait.text, "html.parser")
    wait_csrf = _csrf(swait)
    scope_type = scope_attr = ''
    _onclk_rx = re.compile(
        r"onClkLink\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]",
        re.IGNORECASE)
    for tr in swait.find_all("tr"):
        if RECON_STMT_ROW_LABEL not in tr.get_text():
            continue
        for el in tr.find_all(['a', 'img']):
            for attr in ('href', 'onclick'):
                m = _onclk_rx.search(el.get(attr) or '')
                if m:
                    scope_type, scope_attr = m.group(1), m.group(2)
                    break
            if scope_type:
                break
        if scope_type:
            break
    if not scope_type:
        return {"ok": False, "error": "no onClkLink trigger in wait page row",
                "session": session, "base": base}

    rexe = session.post(
        f"{base}/genericExeWait.do",
        data={
            "scopeType":      scope_type,
            "scopeAttr":      scope_attr,
            "refreshContent": "call",
            "WS_CSRFTOKEN":   wait_csrf,
        },
        headers={"Referer": f"{base}/{PRICE_WAIT_URL}"},
        timeout=30,
    )
    rexe.raise_for_status()

    # Step 4 — GET the data PDF directly. The Show Report anchor in
    # the genericExeWait response points at a per-run wrapper file
    # (e.g. Z30_0_…Z.pdf) that only carries the GoldStandard letterhead.
    # The actual data report lives at a fixed, session-scoped URL —
    # ``Z0_0_ReconciliationStatement1544ZT.pdf`` (note the ZT suffix).
    # WS resolves the file based on the session cookie, so this URL is
    # the same across runs and users while still returning the
    # caller's own report.
    pdf_path_url = "servlet/report?fn=Z0_0_ReconciliationStatement1544ZT.pdf"

    if progress_cb:
        progress_cb("downloading", "Downloading recon PDF…")
    rpdf = session.get(f"{base}/{pdf_path_url}", timeout=120)
    rpdf.raise_for_status()
    fname = pdf_path_url.rsplit("/", 1)[-1]
    save_path = out_dir / fname
    save_path.write_bytes(rpdf.content)
    log.info(f"WS Recon PDF saved: {save_path} ({len(rpdf.content)} bytes)")

    return {
        "ok":         True,
        "completed":  True,
        "pdf_path":   str(save_path),
        "pdf_url":    pdf_path_url,
        "scope_type": scope_type,
        "scope_attr": scope_attr,
        "size_bytes": len(rpdf.content),
        "session":    session,
        "base":       base,
    }


# ============================================================================ #
# ── Benchmark Master bridge ─────────────────────────────────────────────── #
# WS form: /fincrm/viewBenchmarks.do
#   GET                       → list view (rows with linkClicked('CODE'))
#   GET ?mode=create          → empty create form (~15K bytes)
#   POST mode=save            → create new benchmark
#   POST mode=edit&newcode=X  → render edit form pre-populated
#   POST mode=modify          → save edits
#   POST mode=delete          → delete (deleteflag[i] checkboxes)
#
# The list page is plain HTML (one TR per benchmark), no AJAX. Each row has:
#   <a href="javascript:linkClicked('CODE')">CODE</a>  in column 2
#   description in column 3, NSE/BSE/Internal refs in 4-6, type in 7
#
# Required fields on save: newcode (max 13), newdescription (max 100).
# Optional: shortName, newnsereference + series (max 2), newbsereference,
# newinternalreference, newvaluesource, newreference6..9.
# ============================================================================ #

BENCHMARK_LIST_URL = "viewBenchmarks.do"
BENCHMARK_FORM_URL = "viewBenchmarks.do?mode=create"
BENCHMARK_POST_URL = "viewBenchmarks.do"


def _benchmark_form_body(code: str, description: str,
                          short_name: str = '', nse_ref: str = '', series: str = '',
                          bse_ref: str = '', internal_ref: str = '', value_source: str = '',
                          ref6: str = '', ref7: str = '', ref8: str = '', ref9: str = '',
                          mode: str = 'save', csrf: str = '') -> dict:
    """Body for save/modify POST. mode='save' for create, 'modify' for edit."""
    return {
        'newcode':              code,
        'newdescription':       description,
        'shortName':            short_name,
        'newnsereference':      nse_ref,
        'series':               series,
        'newbsereference':      bse_ref,
        'newinternalreference': internal_ref,
        'newvaluesource':       value_source,
        'newreference6':        ref6,
        'newreference7':        ref7,
        'newreference8':        ref8,
        'newreference9':        ref9,
        'mode':                 mode,
        'WS_CSRFTOKEN':         csrf,
        'save':                 'Save',
    }


def ws_list_benchmarks(session=None, base=None,
                       auth_cache_path: str = '') -> dict:
    """GET viewBenchmarks.do and parse every benchmark row.

    Returns: {rows: [{code, description, nse_ref, bse_ref, internal_ref,
                       index_type}, ...], csrf, session, base}

    The list HTML is plain table rows; each <a href="javascript:linkClicked('CODE')">
    starts a row. We walk the table and pull adjacent <td> cell text.
    """
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)
    r = session.get(f"{base}/{BENCHMARK_LIST_URL}", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    rows = []
    for a in soup.find_all('a', href=lambda h: h and 'linkClicked(' in h):
        # href is "javascript:linkClicked('NIFTY50TRI')"
        m = re.search(r"linkClicked\(['\"]([^'\"]+)['\"]\)", a.get('href', ''))
        if not m:
            continue
        code = m.group(1).strip()
        # The <a> sits in a <td> inside a <tr>. Pull the row's tds.
        tr = a.find_parent('tr')
        if not tr:
            continue
        tds = tr.find_all('td')
        # Layout per recon HTML:
        #   td[0] = checkbox, td[1] = code link, td[2] = description,
        #   td[3] = nse, td[4] = bse, td[5] = internal, td[6] = type
        def _cell(i):
            return tds[i].get_text(strip=True) if i < len(tds) else ''
        rows.append({
            'code':         code,
            'description':  _cell(2),
            'nse_ref':      _cell(3),
            'bse_ref':      _cell(4),
            'internal_ref': _cell(5),
            'index_type':   _cell(6),
        })

    csrf_inp = soup.find('input', {'name': 'WS_CSRFTOKEN'})
    csrf = (csrf_inp.get('value') if csrf_inp else '') or ''
    return {'rows': rows, 'csrf': csrf, 'session': session, 'base': base}


def ws_create_benchmark(code: str, description: str, *,
                         short_name: str = '', nse_ref: str = '', series: str = '',
                         bse_ref: str = '', internal_ref: str = '', value_source: str = '',
                         ref6: str = '', ref7: str = '', ref8: str = '', ref9: str = '',
                         session=None, base=None,
                         auth_cache_path: str = '') -> dict:
    """POST viewBenchmarks.do mode=save to create a new Benchmark row.

    Returns: {status: 'created' | 'exists' | 'unclear', http_status, response_text, ...}
    """
    if not code or not description:
        raise ValueError('code and description are required')
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)

    # Step 1: GET the create form to grab a fresh CSRF token.
    r1 = session.get(f"{base}/{BENCHMARK_FORM_URL}", timeout=30)
    r1.raise_for_status()
    soup1 = BeautifulSoup(r1.text, 'html.parser')
    csrf_inp = soup1.find('input', {'name': 'WS_CSRFTOKEN'})
    csrf = (csrf_inp.get('value') if csrf_inp else '') or ''
    if not csrf:
        raise RuntimeError('viewBenchmarks.do?mode=create did not return a CSRF token')

    # Step 2: POST mode=save with the new row.
    body = _benchmark_form_body(
        code=code, description=description, short_name=short_name,
        nse_ref=nse_ref, series=series, bse_ref=bse_ref,
        internal_ref=internal_ref, value_source=value_source,
        ref6=ref6, ref7=ref7, ref8=ref8, ref9=ref9,
        mode='save', csrf=csrf,
    )
    headers = {'Referer': f"{base}/{BENCHMARK_FORM_URL}"}
    r2 = session.post(f"{base}/{BENCHMARK_POST_URL}", data=body,
                      headers=headers, timeout=30)
    r2.raise_for_status()
    txt_low = r2.text.lower()
    if 'successfully completed' in txt_low or 'inserted successfully' in txt_low:
        status = 'created'
    elif 'already exists' in txt_low or 'duplicate' in txt_low:
        status = 'exists'
    else:
        status = 'unclear'
    return {
        'status':        status,
        'http_status':   r2.status_code,
        'response_text': r2.text,
        'code':          code,
        'description':   description,
        'session':       session,
        'base':          base,
    }


def ws_update_benchmark(code: str, description: str, *,
                         short_name: str = '', nse_ref: str = '', series: str = '',
                         bse_ref: str = '', internal_ref: str = '', value_source: str = '',
                         ref6: str = '', ref7: str = '', ref8: str = '', ref9: str = '',
                         session=None, base=None,
                         auth_cache_path: str = '') -> dict:
    """POST viewBenchmarks.do mode=modify for an existing Benchmark.

    Two-step replication of the browser flow:
      1. POST mode=edit&newcode=CODE → renders the edit form (CSRF rotates)
      2. POST mode=modify with updated fields → saves
    """
    if not code:
        raise ValueError('code is required')
    if session is None or base is None:
        session, base = _pc_open_session(auth_cache_path)

    # Step 1: open list, grab CSRF, then POST mode=edit with newcode to load
    # the editable record. The list page itself carries WS_CSRFTOKEN.
    r0 = session.get(f"{base}/{BENCHMARK_LIST_URL}", timeout=30)
    r0.raise_for_status()
    soup0 = BeautifulSoup(r0.text, 'html.parser')
    csrf_list = (soup0.find('input', {'name': 'WS_CSRFTOKEN'}) or {}).get('value', '') if soup0.find('input', {'name': 'WS_CSRFTOKEN'}) else ''
    edit_body = {
        'mode':         'edit',
        'newcode':      code,
        'WS_CSRFTOKEN': csrf_list,
    }
    headers = {'Referer': f"{base}/{BENCHMARK_LIST_URL}"}
    r1 = session.post(f"{base}/{BENCHMARK_POST_URL}", data=edit_body,
                       headers=headers, timeout=30)
    r1.raise_for_status()
    soup1 = BeautifulSoup(r1.text, 'html.parser')
    csrf2 = (soup1.find('input', {'name': 'WS_CSRFTOKEN'}) or {}).get('value', '') if soup1.find('input', {'name': 'WS_CSRFTOKEN'}) else ''
    if not csrf2:
        raise RuntimeError('mode=edit step did not return a CSRF token — code may not exist')

    # Step 2: POST mode=modify with the updated row.
    body = _benchmark_form_body(
        code=code, description=description, short_name=short_name,
        nse_ref=nse_ref, series=series, bse_ref=bse_ref,
        internal_ref=internal_ref, value_source=value_source,
        ref6=ref6, ref7=ref7, ref8=ref8, ref9=ref9,
        mode='modify', csrf=csrf2,
    )
    r2 = session.post(f"{base}/{BENCHMARK_POST_URL}", data=body,
                      headers={'Referer': f"{base}/{BENCHMARK_LIST_URL}"},
                      timeout=30)
    r2.raise_for_status()
    txt_low = r2.text.lower()
    if 'successfully completed' in txt_low or 'modified successfully' in txt_low:
        status = 'modified'
    elif 'no such' in txt_low or 'not found' in txt_low:
        status = 'not_found'
    else:
        status = 'unclear'
    return {
        'status':        status,
        'http_status':   r2.status_code,
        'response_text': r2.text,
        'code':          code,
        'description':   description,
        'session':       session,
        'base':          base,
    }
