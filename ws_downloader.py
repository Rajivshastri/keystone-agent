# ws_downloader.py
# ─────────────────────────────────────────────────────────────────────────────
# WealthSpectrum report downloader — pure Python requests + AES encryption.
#
# WealthSpectrum encrypts credentials client-side with AES-128-CBC before
# posting to j_spring_security_check. We replicate this in Python using the
# cryptography package (already a dependency).
#
# Environment variables (Azure App Service → Configuration):
#   FINCRM_URL   = https://app.thegoldstandard.in/fincrm  (default)
#   FINCRM_USER  = your WS username
#   FINCRM_PASS  = your WS password
# ─────────────────────────────────────────────────────────────────────────────

import os
import re
import base64
import binascii
import logging
import requests as _requests
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def _base_url() -> str:
    return os.environ.get("FINCRM_URL", "https://app.thegoldstandard.in/fincrm").rstrip("/")

def _username() -> str:
    return os.environ.get("FINCRM_USER", "")

def _password() -> str:
    return os.environ.get("FINCRM_PASS", "")

def ws_creds_configured() -> bool:
    return bool(_username() and _password())


def _fmt(d: datetime) -> str:
    return d.strftime("%d/%m/%Y")


# ── Backoffice-query masters (mirror-DB source) ─────────────────────────────
# Discovered via ws_discover_queries.py against the /reportUploadQuery.do
# menu. Each entry becomes both a REPORTS row and a download_specs entry.
#
# Tuple: (display_name, file_stem, queryfile, catg, scope)
#   scope=""   → system-wide master (banks, brokers, schemes, etc.)
#   scope="*"  → client-scoped (walks the full client tree)
#
# NOTE: these 4 Master queries return empty for this tenant and are
# intentionally omitted:
#   EquitySymbolMasterListing, DebtSecurityMaster  — redundant with
#     SecurityDetail.xml (Security Details) which already lists all
#     23k+ equity+debt securities for this firm
#   Benchmark.xml              — firm doesn't track external benchmark NAVs
#   SettlementCalendar.xml     — firm uses system-default settlement, no
#     custom calendar entries
_MASTERS_BOQ = [
    ("Account Master",       "AccountMaster",         "AccountMaster.xml",             "Master", "*"),
    ("Client Details",       "ClientDetail",          "ClientDetail.xml",              "Master", "*"),
    ("Group Master",         "GroupMaster",           "GroupMaster.xml",               "Master", ""),
    ("Scheme Master",        "SchemeMaster",          "SchemeMasterQuery.xml",         "Master", ""),
    ("Billgroup Master",     "BillgroupMaster",       "BillgroupMaster.xml",           "Master", ""),
    ("Issuer Master",        "IssuerMaster",          "IssuerMaster.xml",              "Master", ""),
    ("Bank Master",          "BankMaster",            "Bank_MST.xml",                  "Master", ""),
    ("Broker Master",        "BrokerMaster",          "BrokerMaster.xml",              "Master", ""),
    ("Custody Master",       "CustodyMaster",         "CustodyMaster.xml",             "Master", ""),
    ("Intermediary Master",  "IntermediaryMaster",    "IntermediaryMaster.xml",        "Master", ""),
    ("Benchmark Mapping",    "BenchmarkMapping",      "BenchmarkMapping.xml",          "Master", ""),
    ("Dimension Master",     "DimensionMaster",       "DimensionMasterData.xml",       "Master", ""),
    ("Branch Master",        "BranchMaster",          "BranchMasterBQ.xml",            "Master", ""),
    ("Personnel Master",     "PersonnelMaster",       "PersonnelMasterBQ.xml",         "Master", ""),
    ("RM Master",            "RelmgrMaster",          "RelmgrMaster.xml",              "Master", ""),
    ("Corporate Params",     "Generalparam",          "Generalparam.xml",              "Master", ""),
]

REPORTS = [
    ("Holdings",           "Holdings",            "xls", False, ["holdings"]),
    ("Trade Transactions", "TradeTrans",          "xls", False, ["holdings"]),
    ("Pool Master",        "PoolMaster",          "xls", False, ["holdings", "bank"]),
    ("Custody Interface",  "CustodyInterface",    "xls", False, ["holdings"]),
    ("Bank Book",          "BankBook",            "csv", False, ["bank"]),
    ("Client DP & Bank",   "ClientDPBankDetails", "xls", False, ["bank", "trade"]),
    ("Order Log",          "OrderLog",            "xls", False, ["trade"]),
    ("Security Details",   "SecurityDetails",     "xls", True,  ["trade"]),
] + [
    (display, stem, "xls", False, ["masters"])
    for (display, stem, _qf, _catg, _scope) in _MASTERS_BOQ
]


# ── AES-128-CBC + PBKDF2 encryption (matches WealthSpectrum AESUtil.js) ─────
#
# auth.js logic (now confirmed):
#   passPhrase = randomVal  (hidden field from login page, changes each load)
#   iv   = CryptoJS.lib.WordArray.random(128/8)   → 16 random bytes
#   salt = CryptoJS.lib.WordArray.random(128/8)   → 16 random bytes
#   key  = PBKDF2(passPhrase, salt, {keySize:4, iterations:100})  → 16 bytes
#   ct   = AES-128-CBC(plaintext, key, iv)  with PKCS7 padding
#   output = hex(iv) + ":" + hex(salt) + ":" + base64(ct)

def _ws_encrypt(plaintext: str, passphrase: str) -> str:
    """Replicate AESUtil.encrypt(salt, iv, passPhrase, plainText)."""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import hashes, padding as _padding
    from cryptography.hazmat.backends import default_backend

    salt = os.urandom(16)
    iv   = os.urandom(16)

    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=salt,
                     iterations=100, backend=default_backend())
    key = kdf.derive(passphrase.encode("utf-8"))

    padder = _padding.PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    enc = cipher.encryptor()
    ct  = enc.update(padded) + enc.finalize()

    # Format: iv:salt:base64(ct)  — matches auth.js output
    return (f"{binascii.hexlify(iv).decode()}"
            f":{binascii.hexlify(salt).decode()}"
            f":{base64.b64encode(ct).decode()}")


# ── Login ─────────────────────────────────────────────────────────────────────

def _login(session: _requests.Session, auth_cache: Path) -> None:
    """
    Log in to WealthSpectrum using AES-encrypted credentials.
    Caches the session cookie to auth_cache so subsequent runs skip login.
    """
    import json

    base = _base_url()

    # Try cached session first
    if auth_cache.exists():
        try:
            state = json.loads(auth_cache.read_text())
            cookies = {c["name"]: c["value"] for c in state.get("cookies", [])}
            if cookies:
                session.cookies.update(cookies)
                r = session.get(f"{base}/", timeout=15, allow_redirects=True)
                if ("j_spring_security_check" not in r.url and
                        "auth.do" not in r.url and len(r.text) > 500):
                    log.info("Cached WS session is valid")
                    return
                log.info("Cached session expired")
                session.cookies.clear()
        except Exception as e:
            log.warning(f"Could not use cached session: {e}")
            session.cookies.clear()

    log.info(f"Logging in to WealthSpectrum as {_username()!r}...")

    # Load login page to collect hidden fields (CSRF token, randomVal, etc.)
    r = session.get(f"{base}/", timeout=30)
    r.raise_for_status()

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, "html.parser")
    form_data: dict = {}
    for inp in soup.find_all("input"):
        name = inp.get("name")
        if name and inp.get("type") != "submit":
            form_data[name] = inp.get("value", "")

    # passPhrase = randomVal from the page (changes each page load)
    # This is what auth.js uses: aesUtil.encrypt(salt, iv, uniqueKey, plaintext)
    # where uniqueKey = document.forms[0].randomVal.value
    passphrase = form_data.get("randomVal", "")
    if not passphrase:
        raise RuntimeError("randomVal not found on login page — cannot encrypt credentials")
    log.info(f"Using randomVal as passphrase (len={len(passphrase)})")

    form_data["j_username"] = _ws_encrypt(_username(), passphrase)
    form_data["j_password"] = _ws_encrypt(_password(), passphrase)
    form_data["randomVal"]  = ""  # auth.js clears this before submit

    log.info(f"Posting to j_spring_security_check with {len(form_data)} fields")

    r2 = session.post(
        f"{base}/j_spring_security_check",
        data=form_data, timeout=30, allow_redirects=True
    )
    r2.raise_for_status()

    log.info(f"Login response: url={r2.url} len={len(r2.text)} "
             f"has_links={'href=' in r2.text}")

    # Success: redirected away from the specific auth/login endpoints.
    # Note: WealthSpectrum's main app URL may contain "login" (e.g. loginScreen.do)
    # so we only block the actual auth failure endpoints.
    _fail_urls = ("j_spring_security_check", "auth.do")
    if any(kw in r2.url for kw in _fail_urls):
        raise RuntimeError(
            f"Login failed — still on auth page after POST. "
            f"URL: {r2.url}  Snippet: {r2.text[:300]!r}"
        )

    log.info("Logged in to WealthSpectrum successfully")

    # Cache session cookie
    cookies_list = [
        {"name": c.name, "value": c.value, "domain": c.domain,
         "path": c.path, "expires": -1, "httpOnly": False,
         "secure": c.secure, "sameSite": "Lax"}
        for c in session.cookies
    ]
    auth_cache.parent.mkdir(parents=True, exist_ok=True)
    auth_cache.write_text(json.dumps({"cookies": cookies_list, "origins": []}))
    log.info(f"Session cached to {auth_cache}")


# ── File downloaders ──────────────────────────────────────────────────────────

def _fetch(session: _requests.Session, url: str, base: str) -> bytes:
    if url.startswith("http"):
        full = url
    elif url.startswith("/fincrm/"):
        # reportcall() returns paths like /fincrm/servlet/... but base already
        # ends with /fincrm — strip the leading /fincrm to avoid double path
        root = base.split("/fincrm")[0]
        full = root + url
    else:
        full = f"{base}/{url.lstrip('/')}"
    log.info(f"     Fetching: {full}")
    r = session.get(full, timeout=120)
    r.raise_for_status()
    return r.content


def _backoffice_url(base: str, date_obj: datetime, query_file: str,
                    catg: str, scope: str, from_date: datetime = None) -> str:
    fd = _fmt(from_date or date_obj)
    td = _fmt(date_obj)
    return (
        f"{base}/servlet/query?queryfile={query_file}&catg={catg}"
        f"&fromdate={fd}&todate={td}&begindate={td}"
        f"&reporttype=excel&scope={scope or ''}&scopeId=0&actactivated=-1"
        f"&txtField1=&txtField2=&txtField3=&txtField4=&txtField5="
    )


def _submit_report_url(session: _requests.Session, base: str,
                        form_url: str, form_data: dict) -> str:
    """
    Two-step report download:
    1. GET form_url  → renders the report parameter form
    2. POST form_url → renders result page with a reportcall('...') link
    3. Return the URL inside reportcall() — caller fetches that to get the file
    """
    from bs4 import BeautifulSoup

    # Step 1: GET the form page, collect any hidden fields
    r = session.get(form_url, timeout=30, allow_redirects=True)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    data: dict = {}
    for inp in soup.find_all("input", type="hidden"):
        if inp.get("name"):
            data[inp["name"]] = inp.get("value", "")
    data.update(form_data)

    # Step 2: POST to the same URL (WS report forms self-submit)
    log.info(f"     Submitting report form: {form_url}")
    r2 = session.post(form_url, data=data, timeout=60, allow_redirects=True)
    r2.raise_for_status()

    # Step 3: Parse reportcall('...') from result page
    m = re.search(r"reportcall\('([^']+)'", r2.text)
    if not m:
        raise RuntimeError(
            f"'Show Report' link not found after POST to {form_url}. "
            f"Snippet: {r2.text[:400]!r}"
        )
    return m.group(1)


# ── Custody Interface (multi-step, frame-aware) ───────────────────────────────

def _custody_interface(session: _requests.Session, base: str, fd: str) -> str:
    """
    Custody Interface — three steps to work around chained JS dropdowns:

    Step 1: GET the form URL → accountsel dropdown already visible
    Step 2: GET with accountsel=S → server renders dpCustAc/dpidCustAc fields
    Step 3: POST all fields together → result page with reportcall()
    """
    from bs4 import BeautifulSoup

    # Form lives at protected/reportCustInterface.jsp (not reportCustInterface.do)
    # POST action is reportCustInterface.do
    form_url = (f"{base}/protected/reportCustInterface.jsp"
                f"?scope=*&menuDisp=N&srcMenuId=1978")
    post_url = f"{base}/reportCustInterface.do"
    headers  = {"Referer": f"{base}/loginScreen.do"}

    # Step 1 — GET the form, collect hidden fields + CSRF token
    r1 = session.get(form_url, headers=headers, timeout=30)
    r1.raise_for_status()
    log.info(f"     CustInt form GET: len={len(r1.text)}")

    soup = BeautifulSoup(r1.text, "html.parser")
    hidden: dict = {}
    for inp in soup.find_all("input"):
        name = inp.get("name")
        if name and inp.get("type") != "submit":
            hidden[name] = inp.get("value", "")

    # Step 2 — POST all fields including the chained-select values
    # dpCustAc/dpidCustAc are normally revealed by JS after selecting accountsel=S
    # but the server accepts them in a single POST
    post_data = {
        **hidden,
        "interfaceType": "KOTAK - CUSTODY",
        "reportopt":     "TSB",
        "reportfor":     "BY-,SL+",
        "dataOption":    "",
        "duedate":       fd,
        "scope":         "*",
        "accountsel":    "S",
        "dpCustAc":      "NSDL",
        "dpidCustAc":    "IN303173",
        "mapinid":       "",
        "accountType":   "B",
        "reportFormat":  "X",
        "headerReq":     "Y",
    }
    log.info(f"     CustInt POST: {len(post_data)} fields to {post_url}")
    r2 = session.post(post_url, data=post_data,
                      headers={"Referer": form_url}, timeout=60,
                      allow_redirects=True)
    r2.raise_for_status()
    log.info(f"     CustInt POST response: len={len(r2.text)} snippet={r2.text[:200]!r}")

    m = re.search(r"reportcall\('([^']+)'", r2.text)
    if not m:
        raise RuntimeError(
            f"'Show Report' not found after custody interface POST. "
            f"Snippet: {r2.text[:500]!r}"
        )
    return m.group(1)


# ── Per-custodian Custody Interface download ─────────────────────────────────

def download_custody_interface(session: _requests.Session, base: str,
                               interface_type: str, report_format: str,
                               common_cfg: dict, date_str: str,
                               out_path: Path, mapinid: str = '') -> Path:
    """
    Download the Custody Interface file for a specific custodian.

    interface_type: WS dropdown value (e.g. "ICICI BANK", "KOTAK - CUSTODY")
    report_format:  "XX" (XLSX), "X" (XLS), or "C" (CSV)
    common_cfg:     shared params from custodian_dispatch.json "common" block
    date_str:       DD/MM/YYYY format
    out_path:       where to save the downloaded file

    Returns the path to the saved file.
    """
    from bs4 import BeautifulSoup

    form_url = (f"{base}/protected/reportCustInterface.jsp"
                f"?scope=*&menuDisp=N&srcMenuId=1978")
    post_url = f"{base}/reportCustInterface.do"
    headers  = {"Referer": f"{base}/loginScreen.do"}

    # Step 1 — GET the form, collect hidden fields
    r1 = session.get(form_url, headers=headers, timeout=30)
    r1.raise_for_status()

    soup = BeautifulSoup(r1.text, "html.parser")
    hidden = {}
    for inp in soup.find_all("input"):
        name = inp.get("name")
        if name and inp.get("type") != "submit":
            hidden[name] = inp.get("value", "")

    # Step 2 — POST with common params + per-custodian interface_type/format
    post_data = {
        **hidden,
        "interfaceType": interface_type,
        "reportopt":     common_cfg.get("report_opt", "TSB"),
        "reportfor":     common_cfg.get("report_for", "BY-,SL+"),
        "dataOption":    "",
        "duedate":       date_str,
        "scope":         "*",
        "accountsel":    common_cfg.get("account_sel", "A"),
        "dpCustAc":      "",
        "dpidCustAc":    "",
        "mapinid":       mapinid,
        "accountType":   common_cfg.get("account_type", "B"),
        "reportFormat":  report_format,
        "headerReq":     "Y",
    }
    log.info(f"     CustInt [{interface_type}] POST to {post_url}")
    r2 = session.post(post_url, data=post_data,
                      headers={"Referer": form_url}, timeout=60,
                      allow_redirects=True)
    r2.raise_for_status()

    m = re.search(r"reportcall\('([^']+)'", r2.text)
    if not m:
        raise RuntimeError(
            f"Custody Interface [{interface_type}]: reportcall not found. "
            f"Snippet: {r2.text[:500]!r}"
        )
    report_url = m.group(1)
    data = _fetch(session, report_url, base)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    kb = len(data) // 1024
    log.info(f"     CustInt [{interface_type}] saved: {out_path} ({kb} KB)")
    return out_path


# ── Master runner ─────────────────────────────────────────────────────────────

def run_all_downloads(date_obj: datetime, app_dir: Path,
                      progress_cb=None, reports_filter=None) -> dict:
    """
    Download WS reports for the given date.
    reports_filter: optional list of report display names (e.g. ["Holdings","Bank Book"]).
      If provided, only those reports are downloaded. None/empty → all reports.
    """
    date_str       = date_obj.strftime("%Y-%m-%d")
    date_masters   = app_dir / "data" / date_str / "masters"
    shared_masters = app_dir / "masters"
    auth_cache     = app_dir / "config" / "ws_auth.json"
    date_masters.mkdir(parents=True, exist_ok=True)
    shared_masters.mkdir(parents=True, exist_ok=True)

    base = _base_url()
    fd   = _fmt(date_obj)
    results = []

    session = _requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"{base}/",
    })

    if progress_cb:
        progress_cb("__login__", "running", "Logging in to WealthSpectrum...")
    try:
        _login(session, auth_cache)
    except Exception as e:
        # Retry once after clearing cached session (may be stale)
        log.warning(f"WS login failed, retrying with fresh session: {e}")
        try:
            auth_cache.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            if progress_cb:
                progress_cb("__login__", "running", "Retrying login (stale session)...")
            _login(session, auth_cache)
        except Exception as e2:
            import traceback
            err = f"{type(e2).__name__}: {e2}"
            log.error(f"WS login failed after retry:\n{traceback.format_exc()}")
            if progress_cb:
                progress_cb("__login__", "error", err)
            _fail_list = [r for r in REPORTS if not reports_filter or r[0] in reports_filter]
            return {
                "date": date_str,
                "results": [{"name": r[0], "status": "error",
                             "error": f"Login failed: {err}"} for r in _fail_list],
                "success_count": 0, "total": len(_fail_list), "login_error": err,
            }

    if progress_cb:
        progress_cb("__login__", "ok", "Logged in")

    download_specs = [
        ("Holdings",          "Holdings",            "xls", False,
         _backoffice_url(base, date_obj, "Holding.xml", "", "*")),
        ("Trade Transactions","TradeTrans",           "xls", False,
         _backoffice_url(base, date_obj, "TradeTrans.xml", "", "*", date_obj)),
        ("Pool Master",       "PoolMaster",           "xls", False,
         _backoffice_url(base, date_obj, "PoolMaster.xml", "Master", "")),
        ("Custody Interface", "CustodyInterface",     "xls", False,
         lambda: _custody_interface(session, base, fd)),
        ("Bank Book",         "BankBook",             "csv", False,
         lambda: _submit_report_url(session, base,
             f"{base}/reportCashledger.do?scope=*&cmScope=*&consolidation=C&srcMenuId=178",
             {"scope":"*","consolidate":"P","fromDate":fd,"toDate":fd,
              "layoutstyle":"C","accSelection":"A","reportFormat":"C","outflag":"N"})),
        ("Client DP & Bank",  "ClientDPBankDetails",  "xls", False,
         _backoffice_url(base, date_obj, "ClientDPBankDetails.xml", "Master", "*")),
        ("Order Log",         "OrderLog",             "xls", False,
         _backoffice_url(base, date_obj, "OrderLog.xml", "", "*", date_obj)),
        ("Security Details",  "SecurityDetails",      "xls", True,
         _backoffice_url(base, date_obj, "SecurityDetail.xml", "Master", "")),
    ] + [
        (display, stem, "xls", False,
         _backoffice_url(base, date_obj, qf, catg, scope))
        for (display, stem, qf, catg, scope) in _MASTERS_BOQ
    ]

    if reports_filter:
        _filter_set = set(reports_filter)
        download_specs = [spec for spec in download_specs if spec[0] in _filter_set]

    for label, filename, ext, shared, url_or_fn in download_specs:
        dest_dir  = shared_masters if shared else date_masters
        save_path = dest_dir / f"{filename}.{ext}"
        if progress_cb:
            progress_cb(label, "running", "Downloading...")
        try:
            report_url = url_or_fn() if callable(url_or_fn) else url_or_fn
            data = _fetch(session, report_url, base)
            save_path.write_bytes(data)
            kb = len(data) // 1024
            log.info(f"  OK  {label} -> {save_path} ({kb} KB)")
            if progress_cb:
                progress_cb(label, "ok", f"{kb} KB — saved")
            results.append({"name": label, "status": "ok",
                            "path": str(save_path), "size": len(data)})
        except Exception as e:
            import traceback
            detail = f"{type(e).__name__}: {e}"
            log.error(f"  FAIL  {label}:\n{traceback.format_exc()}")
            if progress_cb:
                progress_cb(label, "error", detail)
            # Invalidate cached session if it looks like auth expired
            if any(x in detail for x in ("401", "403", "login", "session")):
                try: auth_cache.unlink(missing_ok=True)
                except Exception: pass
            results.append({"name": label, "status": "error", "error": detail})

    ok = sum(1 for r in results if r["status"] == "ok")
    return {"date": date_str, "results": results,
            "success_count": ok, "total": len(download_specs)}
