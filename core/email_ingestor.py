"""
Email Ingestor — Microsoft Graph API (M365)
--------------------------------------------
Fetches emails from the shared mailbox, identifies the source
by sender email + subject prefix, downloads the zip attachment,
extracts it to the correct source folder.

Azure App Registration Requirements (one-time IT admin setup):
  1. Register app in Azure Portal → App Registrations
  2. Add API permission: Microsoft Graph → Mail.Read (Application)
  3. Grant admin consent
  4. Create Client Secret
  5. Enter Tenant ID, Client ID, Client Secret into Settings → Azure Config
"""
import os
import logging
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

GRAPH_BASE   = 'https://graph.microsoft.com/v1.0'
TOKEN_URL    = 'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token'
GRAPH_SCOPE  = 'https://graph.microsoft.com/.default'


def _normalize_senders(source: dict) -> list:
    """Return lowercase/stripped sender addresses configured on a source.

    The 'sender_email' field accepts either:
      - a string: "custreport@icicibank.com"                 (legacy)
      - a list  : ["custreport@icicibank.com",
                   "custreport@icici.bank.in"]               (multi-domain)

    Big custodians sometimes send from more than one domain (e.g. ICICI
    toggled between icicibank.com and icici.bank.in), so this accepts
    either shape uniformly.
    """
    raw = source.get('sender_email')
    if isinstance(raw, list):
        return [str(x).lower().strip() for x in raw if x and str(x).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.lower().strip()]
    return []


class EmailIngestor:

    def __init__(self, azure_config: dict):
        self.tenant_id     = azure_config.get('tenant_id', '').strip()
        self.client_id     = azure_config.get('client_id', '').strip()
        self.client_secret = azure_config.get('client_secret', '').strip()
        self.mailbox       = azure_config.get('mailbox', '').strip()
        # Test mode: divert all outgoing email to a single address.
        self.test_mode_email = str(azure_config.get('test_mode_email') or '').strip()
        self._token        = None
        self._token_expiry = None

    def is_configured(self) -> bool:
        return all([self.tenant_id, self.client_id,
                    self.client_secret, self.mailbox])

    def _apply_test_mode(self, recipients: list, body_html: str) -> tuple:
        """When test_mode_email is set, divert outgoing mail there and
        prepend a banner listing the originals."""
        if not self.test_mode_email:
            return recipients, body_html
        original = ', '.join(r.strip() for r in (recipients or []) if r and r.strip()) \
            or '(no recipients)'
        banner = (
            '<div style="background:#FFF3CD;border:1px solid #B06820;'
            'border-radius:4px;padding:10px 14px;margin-bottom:14px;'
            'font-family:Arial,sans-serif;font-size:12px;color:#5C3D11">'
            '<strong>&#9888; TEST MODE</strong> &mdash; would normally have gone to: '
            f'<code style="font-family:monospace">{original}</code>'
            '</div>'
        )
        return [self.test_mode_email], banner + (body_html or '')

    # ------------------------------------------------------------------ #
    #  Auth                                                                 #
    # ------------------------------------------------------------------ #

    def _get_token(self) -> Optional[str]:
        """Get or refresh OAuth2 access token using client credentials."""
        now = datetime.utcnow()
        if self._token and self._token_expiry and now < self._token_expiry:
            return self._token

        url = TOKEN_URL.format(tenant_id=self.tenant_id)
        resp = requests.post(url, data={
            'grant_type':    'client_credentials',
            'client_id':     self.client_id,
            'client_secret': self.client_secret,
            'scope':         GRAPH_SCOPE,
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        self._token = data['access_token']
        self._token_expiry = now + timedelta(seconds=data.get('expires_in', 3600) - 60)
        return self._token

    def _headers(self) -> dict:
        return {'Authorization': f'Bearer {self._get_token()}',
                'Content-Type':  'application/json'}

    # ------------------------------------------------------------------ #
    #  Fetch emails                                                         #
    # ------------------------------------------------------------------ #

    def fetch_for_date(self, date_str: str, sources: List[dict],
                       file_manager, log_callback=None,
                       dt_start_override=None,
                       dt_end_override=None,
                       mailbox_override: str = '') -> List[dict]:
        """
        Fetch all relevant emails for the given date from the shared mailbox.
        Downloads and extracts zip attachments to the correct source folder.

        Args:
            date_str:           'YYYY-MM-DD'
            sources:            List of source config dicts from sources.json
            file_manager:       FileManager instance
            log_callback:       Optional callable(msg) for real-time logging
            dt_start_override:  Optional datetime — overrides the default lookback window
            dt_end_override:    Optional datetime — overrides the default target+2d end
            mailbox_override:   Optional mailbox UPN to use instead of
                                self.mailbox. Used by callers that fan out
                                one Graph call per (mailbox, sources_subset)
                                group when a source declares a non-default
                                mailbox in sources.json (e.g. value_research
                                reads from vr@thegoldstandard.in).

        Returns:
            List of result dicts {source, file, status, message}
        """
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                logger.info(msg)

        results = []
        _saved_this_run: set = set()

        if not self.is_configured():
            msg = "Azure credentials not configured. Please update Settings → Azure Config."
            log(f"ERROR: {msg}")
            return [{'source': 'all', 'status': 'error', 'message': msg}]

        target_dt = datetime.strptime(date_str, '%Y-%m-%d')
        dt_end    = dt_end_override   or (target_dt + timedelta(days=2))
        dt_start  = dt_start_override or (target_dt - timedelta(hours=72))

        dt_start_s = dt_start.strftime('%Y-%m-%dT%H:%M:%SZ')
        dt_end_s   = dt_end.strftime('%Y-%m-%dT%H:%M:%SZ')

        _window_hours = int(round((dt_end - dt_start).total_seconds() / 3600))
        # Per-source mailbox override. Most sources land in the default
        # operations@thegoldstandard.in inbox; Value Research sends to
        # vr@thegoldstandard.in. When a non-empty mailbox_override is
        # passed, every Graph URL in this call uses it.
        mb = (mailbox_override or self.mailbox).strip()
        log(f"Fetching emails from {mb} — window {dt_start_s} to {dt_end_s} "
            f"(~{_window_hours}h)")
        log(f"Matching by sender address and zip filename prefix only")

        # Fetch all messages in the window using pagination. Callers can
        # override the default lookback via dt_start_override / dt_end_override
        # (the incremental fetch path uses a 30-min overlap).
        # Graph API max per page is 100 — follow @odata.nextLink until exhausted
        first_url = (f"{GRAPH_BASE}/users/{mb}/messages"
                     f"?$filter=receivedDateTime ge {dt_start_s} "
                     f"and receivedDateTime lt {dt_end_s}"
                     f"&$select=id,subject,from,hasAttachments,receivedDateTime"
                     f"&$top=100&$orderby=receivedDateTime desc")

        messages = []
        next_url = first_url
        page = 0
        try:
            while next_url:
                page += 1
                resp = requests.get(next_url, headers=self._headers(), timeout=30)
                resp.raise_for_status()
                data = resp.json()
                batch = data.get('value', [])
                messages.extend(batch)
                next_url = data.get('@odata.nextLink')
                if next_url:
                    log(f"  Fetched page {page} ({len(messages)} emails so far), loading next page…")
        except requests.HTTPError as e:
            msg = f"Graph API error: {e.response.status_code} — {e.response.text[:200]}"
            log(f"ERROR: {msg}")
            return [{'source': 'all', 'status': 'error', 'message': msg}]
        except Exception as e:
            msg = f"Network error: {e}"
            log(f"ERROR: {msg}")
            return [{'source': 'all', 'status': 'error', 'message': msg}]

        # Build set of known sender addresses for diagnostic logging
        _known_senders = {addr for s in sources for addr in _normalize_senders(s)}

        with_att = sum(1 for m in messages if m.get('hasAttachments'))
        log(f"Found {len(messages)} email(s) in window across {page} page(s) "
            f"({with_att} with attachments)")
        active_sources = [s['name'] for s in sources if s.get('active', True)]
        log(f"Matching against {len(active_sources)} active source(s): {', '.join(active_sources)}")

        # ── Pre-filter messages and parallelise the per-message
        # /attachments GET. The metadata fetch is the dominant cost on
        # busy days (one round-trip per matched message); pulling 5 in
        # parallel cuts a 7-message day from ~12 s to ~3 s.
        from concurrent.futures import ThreadPoolExecutor

        prefiltered: list = []
        for msg in messages:
            if not msg.get('hasAttachments'):
                _sender = msg.get('from', {}).get('emailAddress', {}).get('address', '').lower()
                if any(ks in _sender for ks in _known_senders if ks):
                    log(f"  ⚠ Known sender but hasAttachments=false: "
                        f"from={_sender}, subject={msg.get('subject', '')[:60]}, "
                        f"received={msg.get('receivedDateTime', '')}")
                continue
            sender  = msg.get('from', {}).get('emailAddress', {}).get('address', '').lower()
            subject = msg.get('subject', '')
            matched_source = self._match_source(sender, subject, sources)
            if not matched_source:
                log(f"  No source match for: from={sender}, subject={subject[:40]}")
                continue
            prefiltered.append((msg, matched_source))

        def _fetch_attachments(item):
            msg, matched = item
            msg_id = msg['id']
            url = f"{GRAPH_BASE}/users/{mb}/messages/{msg_id}/attachments"
            try:
                resp = requests.get(url, headers=self._headers(), timeout=30)
                resp.raise_for_status()
                return msg, matched, resp.json().get('value', []), None
            except Exception as e:
                return msg, matched, None, str(e)

        if prefiltered:
            with ThreadPoolExecutor(max_workers=5) as pool:
                attachments_results = list(pool.map(_fetch_attachments, prefiltered))
        else:
            attachments_results = []

        # ── Process matched messages serially after the parallel fetch.
        # The downstream save / extract code touches FileManager state
        # and writes to disk, so keeping it single-threaded avoids races
        # on the per-source folders.
        for msg, matched_source, attachments, att_err in attachments_results:
            sender  = msg.get('from', {}).get('emailAddress', {}).get('address', '').lower()
            subject = msg.get('subject', '')
            msg_id  = msg['id']
            rcvd    = msg.get('receivedDateTime', '')
            source_name = matched_source['name']
            log(f"  Matched: {source_name} — {subject[:40]} (received: {rcvd[:10]})")
            if att_err is not None:
                results.append({'source': source_name, 'status': 'error',
                                 'message': f"Failed to get attachments: {att_err}"})
                continue

            for att in attachments:
                att_name = att.get('name', '')
                is_bank_source    = matched_source.get('is_bank', False)
                attachment_type   = matched_source.get('attachment_type', 'zip')

                # 'direct' type: accept PDF, XLS, XLSX, CSV directly (no zip needed)
                if attachment_type == 'direct':
                    ext = att_name.lower().rsplit('.', 1)[-1] if '.' in att_name else ''
                    if ext not in ('pdf', 'xls', 'xlsx', 'csv', 'zip'):
                        continue
                    # Fall through to download + save below
                # Holdings sources: zip only. Bank sources: zip or txt.
                elif not is_bank_source:
                    if not att_name.lower().endswith('.zip'):
                        continue
                else:
                    # Bank source: accept configured attachment type
                    if attachment_type in ('txt', 'txt_or_csv'):
                        if not att_name.lower().endswith(('.txt', '.csv')):
                            continue
                    elif not att_name.lower().endswith('.zip'):
                        continue

                # Check zip name prefix if configured (for zip attachments)
                zip_prefix = matched_source.get('zip_name_prefix', '')
                if zip_prefix and not att_name.startswith(zip_prefix):
                    log(f"    Skipping attachment (prefix mismatch): {att_name}")
                    continue

                # Check file prefix if configured (for direct attachments)
                file_prefix = matched_source.get('file_prefix', '')
                if file_prefix and not att_name.lower().startswith(file_prefix.lower()):
                    continue

                # Check file_contains substring match — more resilient than
                # file_prefix when a sender renames files (NSDL renamed the
                # steady file at least twice; "CNSTAT" is the one token that
                # has survived every rename). May be a string or a list.
                fc_raw = matched_source.get('file_contains', '')
                file_contains = ([fc_raw] if isinstance(fc_raw, str) else list(fc_raw or [])) if fc_raw else []
                file_contains = [k for k in file_contains if k]
                if file_contains and not any(
                    k.lower() in att_name.lower() for k in file_contains
                ):
                    continue

                # Download zip content
                content_bytes = att.get('contentBytes')
                if not content_bytes:
                    # Need to fetch content separately for large attachments
                    att_id = att['id']
                    try:
                        dl_url = f"{att_url}/{att_id}/$value"
                        dl_resp = requests.get(dl_url, headers=self._headers(), timeout=60)
                        dl_resp.raise_for_status()
                        import base64
                        zip_bytes = dl_resp.content
                    except Exception as e:
                        results.append({'source': source_name, 'status': 'error',
                                         'message': f"Failed to download {att_name}: {e}"})
                        continue
                else:
                    import base64
                    zip_bytes = base64.b64decode(content_bytes)

                # For Kotak: determine per-strategy subfolder from zip filename
                # e.g. "GOLDSTANDARD WEALTH PVT LTD MYSTIC WEVA.zip" → kotak_MYSTIC_WEVA/
                # e.g. "GOLDSTANDARD WEALTH PVT LTD MYSTIC WEMO.zip" → kotak_MYSTIC_WEMO/
                # This applies to both source='kotak' (holdings) and source='kotak_bank' (bank CSV)
                import re as _re
                if source_name in ('kotak', 'kotak_bank'):
                    fname_norm = att_name.upper().replace(' ', '_')
                    m = _re.search(r'(MYSTIC_\w+|CAUTILYA\w*|ER_INDIA\w*)', fname_norm)
                    if m:
                        strategy_suffix = m.group(1)
                        # Holdings: kotak_MYSTIC_WEVA  |  Bank: kotak_bank_MYSTIC_WEVA
                        prefix = 'kotak_bank_' if source_name == 'kotak_bank' else 'kotak_'
                        dest_folder = f'{prefix}{strategy_suffix}'
                    else:
                        dest_folder = source_name  # fallback: flat folder
                else:
                    dest_folder = source_name

                # Extract to a temp staging folder first, then route each
                # extracted file to the correct holding-date folder based on
                # the date found in the extracted filename (+ offset).
                import tempfile, shutil as _shutil
                offset = matched_source.get('file_date_offset', 0)
                staging_dir = tempfile.mkdtemp(prefix='holdings_staging_')
                try:
                    zip_stage_path = os.path.join(staging_dir, att_name)
                    Path(zip_stage_path).write_bytes(zip_bytes)
                    log(f"    Downloaded: {att_name} ({len(zip_bytes):,} bytes)")

                    attachment_type = matched_source.get('attachment_type', 'zip')

                    # Direct attachments (broker CNs, dealer files, exchange files)
                    # — save as-is, no zip extraction needed.
                    if attachment_type == 'direct':
                        # Derive the effective date from the email received time
                        # minus file_date_offset. This determines both the folder
                        # and the filename date tag.
                        from pathlib import Path as _P
                        from datetime import datetime as _dt, timedelta as _td
                        _stem = _P(att_name).stem
                        _sfx  = _P(att_name).suffix
                        _rcvd_raw = rcvd[:10] if rcvd and len(rcvd) >= 10 else ''
                        try:
                            _rcvd_dt  = _dt.strptime(_rcvd_raw, '%Y-%m-%d').date()
                        except ValueError:
                            _rcvd_dt  = None
                        _eff_dt = _rcvd_dt
                        if _rcvd_dt and offset > 0:
                            _eff_dt = _rcvd_dt - _td(days=offset)
                        # Folder date: effective date if available, else pivot date
                        _folder_date = _eff_dt.strftime('%Y-%m-%d') if _eff_dt else date_str
                        # Filename date tag
                        _date_tag = _eff_dt.strftime('%Y%m%d') if _eff_dt else date_str.replace('-', '')
                        save_name  = f"{_stem}_{_date_tag}{_sfx}" if _date_tag not in _stem else att_name

                        final_dir = str(file_manager.raw_dir(_folder_date, dest_folder))
                        Path(final_dir).mkdir(parents=True, exist_ok=True)
                        final_path = os.path.join(final_dir, save_name)
                        # Newest-email-wins: messages are processed newest-first.
                        if save_name in _saved_this_run:
                            log(f"    Skipping older duplicate: {save_name} (newer version already saved)")
                            continue
                        if os.path.exists(final_path):
                            log(f"    Overwriting (re-fetch): {save_name}")
                        _shutil.move(zip_stage_path, final_path)
                        _saved_this_run.add(save_name)
                        results.append({
                            'source':       dest_folder,
                            'holding_date': _folder_date,
                            'status':       'ok',
                            'files':        [save_name],
                            'message':      f'Saved → {_folder_date}/{dest_folder}/{save_name}',
                        })
                        log(f"    → {_folder_date}/{dest_folder}/{save_name}")
                        continue

                    # AES-256 zips (Axis bank) cannot be extracted by Python's zipfile.
                    # Save the zip directly to raw/ — the parser uses 7z internally.
                    if attachment_type == 'zip_aes256':
                        holding_date = self._holding_date_from_filename(
                            att_name, date_str, offset, received_date=rcvd,
                            is_bank=is_bank_source
                        )
                        final_dir = str(file_manager.raw_dir(holding_date, dest_folder))
                        Path(final_dir).mkdir(parents=True, exist_ok=True)
                        final_path = os.path.join(final_dir, att_name)
                        if os.path.exists(final_path):
                            log(f"    Skipping (already have newer): {att_name}")
                            continue
                        _shutil.move(zip_stage_path, final_path)
                        results.append({
                            'source':       dest_folder,
                            'holding_date': holding_date,
                            'status':       'ok',
                            'zip':          att_name,
                            'files':        [att_name],
                            'message':      f'Saved AES-256 zip → {holding_date}/{dest_folder}/{att_name}',
                        })
                        log(f"    → {holding_date}/{dest_folder}/{att_name} (AES-256, saved for 7z parsing)")
                        continue

                    # Plain text/CSV attachments (ICICI bank) — save directly, no extraction.
                    if attachment_type in ('txt', 'txt_or_csv'):
                        holding_date = self._holding_date_from_filename(
                            att_name, date_str, offset, received_date=rcvd,
                            is_bank=is_bank_source
                        )
                        final_dir = str(file_manager.raw_dir(holding_date, dest_folder))
                        Path(final_dir).mkdir(parents=True, exist_ok=True)
                        final_path = os.path.join(final_dir, att_name)
                        if os.path.exists(final_path):
                            log(f"    Skipping (already have newer): {att_name}")
                            continue
                        _shutil.move(zip_stage_path, final_path)
                        results.append({
                            'source':       dest_folder,
                            'holding_date': holding_date,
                            'status':       'ok',
                            'files':        [att_name],
                            'message':      f'Saved TXT → {holding_date}/{dest_folder}/{att_name}',
                        })
                        log(f"    → {holding_date}/{dest_folder}/{att_name}")
                        continue

                    # Standard zip: extract with Python zipfile, route each file
                    zip_password = matched_source.get('zip_password', '')
                    extracted, err = file_manager.extract_zip(
                        zip_stage_path, staging_dir, zip_password
                    )
                    if err:
                        results.append({'source': source_name, 'status': 'error',
                                         'message': f"Zip extraction failed: {err}"})
                        log(f"    ERROR extracting: {err}")
                        continue

                    routed_files = []
                    # Derive a zip alias from the zip attachment name for bank sources.
                    # e.g. "GWPJ.zip" → prefix "GWPJ", "GWPJ0016.zip" → prefix "GWPJ0016"
                    # This is stored as a prefix on extracted filenames so parsers can
                    # recover the pool context: GWPJ_fcat_hdfccustodystmt.xlsx → alias "GWPJ"
                    zip_stem = os.path.splitext(att_name)[0]
                    is_bank_source_flag = matched_source.get('is_bank', False)

                    # Kotak zips: filename date is unreliable (sometimes T+1,
                    # sometimes correct). The CSV inside the zip has a reliable
                    # "Statement from, <DATE>, To, <TO_DATE>" on row 2.
                    # Read the CSV to_date as the authoritative data date.
                    _kotak_zip_date = None
                    if source_name in ('kotak', 'kotak_bank'):
                        for _ep in extracted:
                            if _ep.lower().endswith('.csv'):
                                try:
                                    with open(_ep, 'r', encoding='utf-8-sig', errors='replace') as _kf:
                                        _lines = [_kf.readline() for _ in range(3)]
                                    # Row 2: "Statement from,02-Apr-2026,To,02-Apr-2026"
                                    for _kl in _lines:
                                        _parts = [p.strip() for p in _kl.split(',')]
                                        if len(_parts) >= 4 and _parts[2].lower() == 'to':
                                            _to_raw = _parts[3]
                                            from datetime import datetime as _kdt
                                            for _kfmt in ('%d-%b-%Y', '%d/%m/%Y', '%d-%m-%Y'):
                                                try:
                                                    _to_dt = _kdt.strptime(_to_raw, _kfmt)
                                                    _kotak_zip_date = _to_dt.strftime('%Y-%m-%d')
                                                    break
                                                except ValueError:
                                                    pass
                                            if _kotak_zip_date:
                                                log(f"    Kotak CSV to_date: {_to_raw} → {_kotak_zip_date}")
                                                break
                                except Exception as _ke:
                                    log(f"    Kotak CSV date read failed: {_ke}")
                        # Fallback: open XLSX and read "Market Price Date" (column N)
                        if not _kotak_zip_date:
                            for _ep in extracted:
                                if _ep.lower().endswith('.xlsx'):
                                    try:
                                        import openpyxl as _opx
                                        _kwb = _opx.load_workbook(_ep, read_only=True, data_only=True)
                                        _kws = _kwb[_kwb.sheetnames[0]]
                                        # Find "Market Price Date" column (expected col N = index 14)
                                        _hdr_row = next(_kws.iter_rows(min_row=1, max_row=1, values_only=True), None)
                                        _mpd_col = None
                                        if _hdr_row:
                                            for _ci, _cv in enumerate(_hdr_row):
                                                if _cv and 'market price date' in str(_cv).lower():
                                                    _mpd_col = _ci
                                                    break
                                        if _mpd_col is not None:
                                            for _kr in _kws.iter_rows(min_row=2, max_row=2, values_only=True):
                                                _mpd_val = _kr[_mpd_col] if _mpd_col < len(_kr) else None
                                                if _mpd_val:
                                                    from datetime import datetime as _kdt2
                                                    if hasattr(_mpd_val, 'strftime'):
                                                        _kotak_zip_date = _mpd_val.strftime('%Y-%m-%d')
                                                    else:
                                                        for _kfmt2 in ('%d-%b-%Y', '%d/%m/%Y', '%d-%m-%Y'):
                                                            try:
                                                                _kotak_zip_date = _kdt2.strptime(str(_mpd_val).strip(), _kfmt2).strftime('%Y-%m-%d')
                                                                break
                                                            except ValueError:
                                                                pass
                                                    if _kotak_zip_date:
                                                        log(f"    Kotak XLSX Market Price Date: {_mpd_val} → {_kotak_zip_date}")
                                                break
                                        _kwb.close()
                                    except Exception as _ke2:
                                        log(f"    Kotak XLSX date read failed: {_ke2}")
                                    if _kotak_zip_date:
                                        break

                    for extracted_path in extracted:
                        fname = os.path.basename(extracted_path)
                        # For holdings sources: skip csv and zip (bank statement, nested zip)
                        # For bank sources: keep everything (csv IS the bank statement)
                        # Exception: Kotak zips contain both XLSX (holdings) and CSV
                        # (bank statement) — keep CSVs so bank recon can find them.
                        is_bank_source = is_bank_source_flag
                        is_kotak = source_name in ('kotak', 'kotak_bank')
                        if not is_bank_source and not is_kotak and fname.lower().endswith(('.zip', '.csv')):
                            continue
                        if fname.lower().endswith('.zip'):
                            continue   # never route nested zips regardless
                        # Prefix bank files with the zip stem to preserve pool context.
                        # e.g. fcat_hdfccustodystmt.xlsx → GWPJ_fcat_hdfccustodystmt.xlsx
                        # Skip prefix for Kotak (already has meaningful CSV name like 6651091725.csv)
                        # and for ICICI/Axis which don't use zip_alias for resolution.
                        if (is_bank_source and zip_stem
                                and source_name in ('hdfc_bank', 'hdfc_bank_balance')
                                and not fname.upper().startswith(zip_stem.upper())):
                            fname = f'{zip_stem}_{fname}'
                        # Derive holding date from extracted filename.
                        # For Kotak: use the CSV to_date for ALL files (both
                        # XLSX and CSV) since the filename date is unreliable.
                        if _kotak_zip_date:
                            holding_date = _kotak_zip_date
                        else:
                            holding_date = self._holding_date_from_filename(
                                fname, date_str, offset, received_date=rcvd,
                                is_bank=is_bank_source_flag
                            )
                        final_dir = str(file_manager.raw_dir(holding_date, dest_folder))
                        Path(final_dir).mkdir(parents=True, exist_ok=True)
                        final_path = os.path.join(final_dir, fname)
                        # For bank sources: don't overwrite — the first-written file comes
                        # from the newest email (emails are processed newest-first).
                        # Overwriting would replace current-day data with older-day data.
                        if os.path.exists(final_path):
                            if is_bank_source:
                                log(f"    Skipping (already have newer): {fname}")
                                continue
                            else:
                                os.remove(final_path)
                        _shutil.move(extracted_path, final_path)
                        routed_files.append((fname, holding_date))
                        log(f"    → {holding_date}/{dest_folder}/{fname}")

                    if routed_files:
                        # Group by holding date for the result summary
                        by_date = {}
                        for fname, hdate in routed_files:
                            by_date.setdefault(hdate, []).append(fname)
                        for hdate, fnames in by_date.items():
                            results.append({
                                'source':       dest_folder,
                                'holding_date': hdate,
                                'status':       'ok',
                                'zip':          att_name,
                                'files':        fnames,
                                'message':      f"Routed {len(fnames)} file(s) → {hdate}/{dest_folder}/"
                            })
                finally:
                    _shutil.rmtree(staging_dir, ignore_errors=True)

        if not results:
            log("No matching emails found for configured sources on this date.")

        return results

    def fetch_for_range(self, date_from: str, date_to: str,
                        sources: List[dict], file_manager,
                        log_callback=None, since: str = None,
                        mailbox_override: str = '') -> List[dict]:
        """
        Fetch all emails across a date range.

        If `since` is provided (ISO timestamp from the last fetch log), a single
        Graph API query covers (since − 30 min) → (date_to + 2 days). This is
        the incremental path used by scheduled and manual fetches.

        If `since` is None (admin override or first-ever fetch), falls back to
        per-date pivot stepping across the full range.

        Returns a combined list of result dicts (same format as fetch_for_date).
        """
        from datetime import datetime as _dt, timedelta as _td

        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                logger.info(msg)

        if not self.is_configured():
            return [{'source': 'all', 'status': 'error',
                     'message': 'Azure credentials not configured.'}]

        # --- Incremental fetch: single query from last fetch time ---
        if since:
            try:
                since_dt = _dt.fromisoformat(since.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                since_dt = None

            if since_dt:
                # Strip timezone info for naive comparison
                since_naive = since_dt.replace(tzinfo=None)
                dt_start = since_naive - _td(minutes=30)
                dt_to    = _dt.strptime(date_to, '%Y-%m-%d')
                dt_end   = dt_to + _td(days=2)

                log(f"Incremental fetch: since {since[:16]} "
                    f"(query window {dt_start.strftime('%Y-%m-%dT%H:%M:%SZ')} "
                    f"→ {dt_end.strftime('%Y-%m-%dT%H:%M:%SZ')})")

                return self.fetch_for_date(
                    date_to, sources, file_manager,
                    log_callback=log_callback,
                    dt_start_override=dt_start,
                    dt_end_override=dt_end,
                    mailbox_override=mailbox_override,
                )

        # --- Full fetch: 3-day pivot stepping (admin override / first fetch) ---
        dt_from = _dt.strptime(date_from, '%Y-%m-%d')
        dt_to   = _dt.strptime(date_to,   '%Y-%m-%d')

        pivot_dates = []
        current = dt_to
        while current >= dt_from:
            pivot_dates.append(current.strftime('%Y-%m-%d'))
            current -= _td(days=3)
        if dt_from.strftime('%Y-%m-%d') not in pivot_dates:
            pivot_dates.append(date_from)

        log(f"Full range fetch {date_from} → {date_to}: "
            f"{len(pivot_dates)} search window(s)")

        all_results = []
        for pivot in pivot_dates:
            log(f"--- Fetching window centred on {pivot} ---")
            batch = self.fetch_for_date(pivot, sources, file_manager,
                                        log_callback=log_callback,
                                        mailbox_override=mailbox_override)
            all_results.extend(batch)

        return all_results

    # ------------------------------------------------------------------ #
    #  Source matching                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _holding_date_from_filename(filename: str, requested_date: str,
                                    offset: int,
                                    received_date: str = '',
                                    is_bank: bool = False) -> str:
        """
        Derive the data date from a filename + email metadata.

        For **bank statements** (is_bank=True):
          Bank statements are always T+1 — received the day after the data date.
          1. Primary: email received date minus 1 day (most reliable)
          2. Fallback: date found in filename minus offset
          3. No further fallback — missing date should be flagged

        For **holdings / other sources** (is_bank=False):
          1. Date found in filename minus offset
          2. Fallback: email received date minus offset (if offset > 0)
          3. Final fallback: requested_date (pivot date of the fetch)
        """
        import re
        from datetime import datetime, timedelta

        # ── Bank statements: received date - 1 is primary ────────────
        # Graph API returns receivedDateTime in UTC.  Convert to IST
        # (UTC+5:30) before extracting the calendar date so that an email
        # arriving at e.g. 00:30 IST (= 19:00 UTC previous day) is still
        # attributed to the correct IST date.
        if is_bank and received_date:
            try:
                rcvd_dt = datetime.strptime(received_date[:19], '%Y-%m-%dT%H:%M:%S')
                rcvd_ist = rcvd_dt + timedelta(hours=5, minutes=30)
                return (rcvd_ist - timedelta(days=1)).strftime('%Y-%m-%d')
            except ValueError:
                pass

        # ── Date patterns in filename ─────────────────────────────────
        patterns = [
            (r'(\d{2})(\d{2})(\d{4})', '%d%m%Y'),   # DDMMYYYY e.g. 18032026
            (r'(\d{4})(\d{2})(\d{2})', '%Y%m%d'),   # YYYYMMDD e.g. 20260318
            (r'(\d{1,2})[_\-](\d{1,2})[_\-](\d{4})', None),  # D_M_YYYY / DD-MM-YYYY
        ]

        for pattern, fmt in patterns:
            for m in re.finditer(pattern, filename):
                try:
                    if fmt:
                        file_dt = datetime.strptime(m.group(0), fmt)
                    else:
                        g = m.groups()
                        file_dt = datetime.strptime(
                            f"{g[0].zfill(2)}/{g[1].zfill(2)}/{g[2]}", '%d/%m/%Y'
                        )
                    req_dt = datetime.strptime(requested_date, '%Y-%m-%d')
                    if abs((file_dt - req_dt).days) <= 7:
                        holding_dt = file_dt - timedelta(days=offset)
                        return holding_dt.strftime('%Y-%m-%d')
                except (ValueError, IndexError):
                    continue

        # ── Non-bank fallback: received date with offset ──────────────
        if not is_bank and offset and received_date:
            try:
                rcvd_dt = datetime.strptime(received_date[:19], '%Y-%m-%dT%H:%M:%S')
                rcvd_ist = rcvd_dt + timedelta(hours=5, minutes=30)
                return (rcvd_ist - timedelta(days=offset)).strftime('%Y-%m-%d')
            except ValueError:
                pass

        return requested_date


    def _match_source(self, sender: str, subject: str,
                      sources: List[dict]) -> Optional[dict]:
        """
        Match an email to a source config.

        Matching rules (all configured criteria must pass):
          - sender_email:    case-insensitive substring of the From address
          - subject_keyword: case-insensitive substring anywhere in the subject
          - subject_prefix:  case-insensitive match at the start of subject

        sender_email + subject_keyword together is the recommended setup —
        it avoids pulling in other emails from the same sender domain.
        """
        for source in sources:
            if not source.get('active', True):
                continue

            configured_senders  = _normalize_senders(source)
            subject_keyword     = source.get('subject_keyword', '').strip()
            subject_prefix      = source.get('subject_prefix', '').strip()

            # At least one criterion must be configured
            if not configured_senders and not subject_keyword and not subject_prefix:
                continue

            # Sender check — any configured address appears as substring
            if configured_senders and not any(
                cs in sender.lower() for cs in configured_senders
            ):
                continue

            # Subject keyword — must appear anywhere in subject (case-insensitive)
            if subject_keyword and subject_keyword.lower() not in subject.lower():
                continue

            # Subject prefix — must match start of subject (case-insensitive)
            if subject_prefix and not subject.lower().startswith(subject_prefix.lower()):
                continue

            logger.debug(f"Matched '{sender}' / '{subject[:40]}' to source '{source['name']}'")
            return source
        return None


    def send_recon_summary(self, results: dict, date_str: str,
                           recipients: list,
                           attachment_path: str = None,
                           comments: list = None) -> dict:
        """
        Send a reconciliation summary email via Microsoft Graph API.
        Requires Mail.Send permission on the Azure app registration.

        Args:
            results:    Recon results dict {category: [rows]}
            date_str:   'YYYY-MM-DD'
            recipients: List of email address strings
        """
        if not self.is_configured():
            return {'ok': False, 'message': 'Azure credentials not configured.'}
        if not recipients:
            return {'ok': False, 'message': 'No recipients specified.'}

        from datetime import datetime as _dt
        display_date = _dt.strptime(date_str, '%Y-%m-%d').strftime('%d-%m-%Y')

        cats = [
            ('unexplained',       '&#x2717; Unexplained Breaks',     '#B03030', '#FDF1F1'),
            ('minor_break',       '&#x26A0; Minor Breaks (&lt;1)',    '#B06820', '#FFF8E1'),
            ('custody_only',      '+ Custody Only',                   '#2A4FA8', '#EEF4FF'),
            ('ws_only',           '&#x2212; WS Only',                 '#E65100', '#FFF3E0'),
            ('pending_explained', '&#x7E; Pending Explained',         '#B06820', '#FDF5EB'),
            ('clean',             '&#x2713; Clean Matches',           '#1A7A4A', '#EAF7F0'),
        ]

        total     = sum(len(v) for v in results.values())
        has_breaks = bool(results.get('unexplained') or
                          results.get('custody_only') or
                          results.get('ws_only'))
        status_c  = '#B03030' if has_breaks else '#1A7A4A'
        status_t  = 'ACTION REQUIRED' if has_breaks else 'ALL CLEAR'

        # Summary table rows
        trows = ''
        for cat, label, fg, bg in cats:
            count = len(results.get(cat, []))
            bold  = 'font-weight:bold;' if count > 0 and cat in (
                'unexplained', 'custody_only', 'ws_only') else ''
            trows += (
                f'<tr><td style="padding:8px 14px;border-bottom:1px solid #e0e5ed;'
                f'background:{bg};color:{fg};{bold}">{label}</td>'
                f'<td style="padding:8px 14px;border-bottom:1px solid #e0e5ed;'
                f'background:{bg};color:{fg};text-align:center;font-weight:bold">'
                f'{count}</td></tr>'
            )

        # Break detail (unexplained, up to 20)
        brows = ''
        for row in results.get('unexplained', [])[:20]:
            brk = row.get('logical_break', 0)
            brows += (
                f'<tr>'
                f'<td style="padding:6px 10px;border-bottom:1px solid #fde;'
                f'font-family:monospace;font-size:12px">{row.get("client","")}</td>'
                f'<td style="padding:6px 10px;border-bottom:1px solid #fde;'
                f'font-family:monospace;font-size:12px">{row.get("isin","")}</td>'
                f'<td style="padding:6px 10px;border-bottom:1px solid #fde;'
                f'font-size:12px">{str(row.get("security_name",""))[:40]}</td>'
                f'<td style="padding:6px 10px;border-bottom:1px solid #fde;'
                f'text-align:right;color:#B03030;font-weight:bold;'
                f'font-family:monospace;font-size:12px">{brk:+.4f}</td>'
                f'</tr>'
            )

        break_section = ''
        if brows:
            n    = len(results.get('unexplained', []))
            note = f' (showing first 20 of {n})' if n > 20 else ''
            break_section = (
                f'<h3 style="color:#B03030;font-family:Arial,sans-serif;'
                f'margin:24px 0 8px">Unexplained Breaks{note}</h3>'
                f'<table style="border-collapse:collapse;width:100%;'
                f'font-family:Arial,sans-serif">'
                f'<thead><tr style="background:#B03030;color:white">'
                f'<th style="padding:8px 10px;text-align:left">Client</th>'
                f'<th style="padding:8px 10px;text-align:left">ISIN</th>'
                f'<th style="padding:8px 10px;text-align:left">Security</th>'
                f'<th style="padding:8px 10px;text-align:right">Break</th>'
                f'</tr></thead><tbody>{brows}</tbody></table>'
            )

        # Comments section for email
        comments_section = ''
        if comments:
            crows = ''
            for c in comments:
                crows += (
                    f'<tr>'
                    f'<td style="padding:6px 10px;border-bottom:1px solid #e8edf5;'
                    f'font-family:monospace;font-size:12px">{c.get("client","")}</td>'
                    f'<td style="padding:6px 10px;border-bottom:1px solid #e8edf5;'
                    f'font-size:12px">{str(c.get("security_name",""))[:35]}</td>'
                    f'<td style="padding:6px 10px;border-bottom:1px solid #e8edf5;'
                    f'text-align:right;font-family:monospace;font-size:12px;color:#B03030">'
                    f'{c.get("logical_break",0):+.4f}</td>'
                    f'<td style="padding:6px 10px;border-bottom:1px solid #e8edf5;'
                    f'font-size:12px;color:#444">{c.get("comment","")}</td>'
                    f'</tr>'
                )
            comments_section = (
                f'<h3 style="color:#1B2A4A;font-family:Arial,sans-serif;margin:24px 0 8px">'
                f'Unexplained Break Comments</h3>'
                f'<table style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif">'
                f'<thead><tr style="background:#1B2A4A;color:white">'
                f'<th style="padding:8px 10px;text-align:left">Client</th>'
                f'<th style="padding:8px 10px;text-align:left">Security</th>'
                f'<th style="padding:8px 10px;text-align:right">Break</th>'
                f'<th style="padding:8px 10px;text-align:left">Comment</th>'
                f'</tr></thead><tbody>{crows}</tbody></table>'
            )

        html_body = (
            '<div style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto">'
            f'<div style="background:#1B2A4A;padding:20px 24px;border-bottom:3px solid #C9A84C">'
            f'<h1 style="color:white;margin:0;font-size:20px">'
            f'Holdings Reconciliation &#8212; {display_date}</h1>'
            f'<p style="color:#C9A84C;margin:6px 0 0;font-size:13px">'
            f'Status: <span style="color:{status_c};background:white;'
            f'padding:2px 10px;border-radius:4px;font-weight:bold">{status_t}</span></p>'
            f'</div>'
            f'<div style="padding:20px 0">'
            f'<h2 style="color:#1B2A4A;font-size:15px;margin:0 0 12px">Summary</h2>'
            f'<table style="border-collapse:collapse;width:100%">'
            f'<thead><tr style="background:#1B2A4A">'
            f'<th style="padding:10px 14px;text-align:left;color:white;font-size:12px">Category</th>'
            f'<th style="padding:10px 14px;text-align:center;color:white;font-size:12px;width:80px">Count</th>'
            f'</tr></thead>'
            f'<tbody>{trows}'
            f'<tr style="background:#f0f2f5">'
            f'<td style="padding:8px 14px;font-weight:bold">Total Positions</td>'
            f'<td style="padding:8px 14px;text-align:center;font-weight:bold">{total}</td>'
            f'</tr></tbody></table></div>'
            f'{break_section}'
            f'{comments_section}'
            f'<p style="color:#888;font-size:11px;margin-top:24px;'
            f'border-top:1px solid #e0e5ed;padding-top:12px">'
            f'Generated by Keystone &#8212; GoldStandard Wealth Pvt Ltd<br>'
            f'Full reconciliation report is attached.</p></div>'
        )

        # Subject prefix:
        #   [EXPLAINED] — final email with operator comments attached
        #   [ACTION]    — initial alert, breaks still pending explanation
        #   [OK]        — no breaks
        if has_breaks and comments:
            _subject_prefix = '[EXPLAINED] '
        elif has_breaks:
            _subject_prefix = '[ACTION] '
        else:
            _subject_prefix = '[OK] '
        subject = f'{_subject_prefix}Holdings Recon {display_date} &#8212; {status_t}'

        # Build attachments list if file provided
        attachments = []
        if attachment_path and os.path.exists(attachment_path):
            try:
                import base64 as _b64
                with open(attachment_path, 'rb') as _f:
                    file_bytes = _f.read()
                attachments = [{
                    '@odata.type':  '#microsoft.graph.fileAttachment',
                    'name':         os.path.basename(attachment_path),
                    'contentType':  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    'contentBytes': _b64.b64encode(file_bytes).decode('utf-8'),
                }]
                logger.info(f"Attaching file: {os.path.basename(attachment_path)} ({len(file_bytes):,} bytes)")
            except Exception as e:
                logger.warning(f"Could not attach file: {e}")

        # Test-mode rewrite — diverts to a single address when configured.
        recipients, html_body = self._apply_test_mode(recipients, html_body)

        payload = {
            'message': {
                'subject': subject,
                'body': {'contentType': 'HTML', 'content': html_body},
                'toRecipients': [
                    {'emailAddress': {'address': r.strip()}}
                    for r in recipients if r.strip()
                ],
                'attachments': attachments,
            },
            'saveToSentItems': True,
        }

        url = f"{GRAPH_BASE}/users/{self.mailbox}/sendMail"
        try:
            resp = requests.post(url, headers=self._headers(),
                                 json=payload, timeout=30)
            resp.raise_for_status()
            logger.info(f"Recon summary email sent to: {recipients}")
            return {'ok': True,
                    'message': f"Email sent to {', '.join(recipients)}"}
        except requests.HTTPError as e:
            msg = (f"Graph API error {e.response.status_code}: "
                   f"{e.response.text[:300]}")
            logger.error(msg)
            return {'ok': False, 'message': msg}
        except Exception as e:
            logger.error(f"Email send failed: {e}")
            return {'ok': False, 'message': str(e)}

    def send_bank_recon_summary(self, summary_dict: dict, date_str: str,
                                recipients: list,
                                attachment_path: str = None,
                                is_final: bool = False) -> dict:
        """
        Send a Bank Reconciliation summary email via Microsoft Graph API.

        Args:
            summary_dict:  BankReconSummary.to_dict() output
            date_str:      'YYYY-MM-DD'
            recipients:    List of email address strings
            attachment_path: Optional path to the exported Excel file
        """
        if not self.is_configured():
            return {'ok': False, 'message': 'Azure credentials not configured.'}
        if not recipients:
            return {'ok': False, 'message': 'No recipients specified.'}

        from datetime import datetime as _dt
        display_date = _dt.strptime(date_str, '%Y-%m-%d').strftime('%d-%m-%Y')

        n_clean   = summary_dict.get('clean',       0)
        n_breaks  = summary_dict.get('breaks',      0)
        n_total   = summary_dict.get('total_pools', 0)
        has_breaks = n_breaks > 0
        status_t  = 'ACTION REQUIRED' if has_breaks else 'ALL CLEAR'
        status_c  = '#B03030' if has_breaks else '#1A7A4A'

        # ── KPI stat boxes ─────────────────────────────────────────────
        def _stat(label, value, bg, fg):
            return (
                f'<td style="padding:12px 20px;text-align:center;background:{bg};'
                f'border-right:1px solid #e0e5ed">'
                f'<div style="font-size:26px;font-weight:700;color:{fg}">{value}</div>'
                f'<div style="font-size:10px;color:{fg};text-transform:uppercase;'
                f'letter-spacing:.07em;margin-top:2px">{label}</div></td>'
            )

        kpi_row = (
            '<table style="border-collapse:collapse;width:100%;'
            'border:1px solid #e0e5ed;border-radius:6px;overflow:hidden;margin-bottom:20px">'
            '<tr>'
            + _stat('Clean',   n_clean,   '#eaf7ef', '#1a7a4a')
            + _stat('Breaks',  n_breaks,  '#fdf1f1' if has_breaks else '#f5f5f5',
                    '#c0392b' if has_breaks else '#888')
            + _stat('Total Pools', n_total, '#f4f6fc', '#1B2A4A')
            + '</tr></table>'
        )

        # ── Pool detail table ──────────────────────────────────────────
        STATUS_COLOUR = {
            'Clean':              ('#eaf7ef', '#1a7a4a'),
            'Balance Break':      ('#fdf1f1', '#c0392b'),
            'Transaction Break':  ('#fff8e6', '#b06820'),
            'No Statement':       ('#f5f5f5', '#666666'),
            'Not in WS':          ('#f0f4ff', '#3d5a99'),
        }
        TOLERANCE = 0.05

        def _email_sort_key(pr):
            s = pr.get('overall_status', '')
            v = abs(pr.get('l1_variance', 0.0) or 0.0)
            if s == 'No Statement':      return (0, 0,  pr.get('bank',''), pr.get('strategy_name',''))
            if s == 'Balance Break':     return (1, -v, pr.get('bank',''), pr.get('strategy_name',''))
            if s == 'Transaction Break': return (2, -v, pr.get('bank',''), pr.get('strategy_name',''))
            if s == 'Clean' and v > 0:   return (3, -v, pr.get('bank',''), pr.get('strategy_name',''))
            if s == 'Clean':             return (4, 0,  pr.get('bank',''), pr.get('strategy_name',''))
            return (5, 0, pr.get('bank',''), pr.get('strategy_name',''))

        pool_rows = ''
        for pr in sorted(summary_dict.get('pool_results', []), key=_email_sort_key):
            status = pr.get('overall_status', '')
            # Amber override for Clean with sub-tolerance variance
            variance = pr.get('l1_variance', 0.0) or 0.0
            is_amber_clean = (status == 'Clean' and 0 < abs(variance) <= TOLERANCE)
            if is_amber_clean:
                bg, fg = '#fffde7', '#856404'
            else:
                bg, fg = STATUS_COLOUR.get(status, ('#f5f5f5', '#333'))
            var_str  = (f'<span style="color:#c0392b;font-weight:700">'
                        f'₹{variance:+,.2f}</span>'
                        if abs(variance) > 0 else
                        '<span style="color:#1a7a4a">—</span>')
            pool_rows += (
                f'<tr>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #e8edf5;'
                f'font-size:12px">{pr.get("strategy_name","")}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #e8edf5;'
                f'font-size:12px;color:#556">{pr.get("bank","")}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #e8edf5;'
                f'font-family:monospace;font-size:12px">{pr.get("cust_account","")}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #e8edf5;'
                f'text-align:right;font-family:monospace;font-size:12px">'
                f'₹{pr.get("cust_closing",0):,.2f}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #e8edf5;'
                f'text-align:right;font-family:monospace;font-size:12px">'
                f'₹{pr.get("ws_closing_sum",0):,.2f}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #e8edf5;'
                f'text-align:right">{var_str}</td>'
                f'<td style="padding:8px 12px;border-bottom:1px solid #e8edf5;'
                f'text-align:center">'
                f'<span style="background:{bg};color:{fg};padding:2px 9px;'
                f'border-radius:10px;font-size:11px;font-weight:700">{status}</span>'
                f'</td>'
                f'</tr>'
            )

        pool_table = (
            '<h3 style="color:#1B2A4A;font-family:Arial,sans-serif;margin:20px 0 8px">'
            'Pool Summary</h3>'
            '<table style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif">'
            '<thead><tr style="background:#1B2A4A;color:white">'
            '<th style="padding:9px 12px;text-align:left;font-size:11px">Strategy</th>'
            '<th style="padding:9px 12px;text-align:left;font-size:11px">Bank</th>'
            '<th style="padding:9px 12px;text-align:left;font-size:11px">Pool Account</th>'
            '<th style="padding:9px 12px;text-align:right;font-size:11px">Cust Closing (₹)</th>'
            '<th style="padding:9px 12px;text-align:right;font-size:11px">WS Closing (₹)</th>'
            '<th style="padding:9px 12px;text-align:right;font-size:11px">Variance (₹)</th>'
            '<th style="padding:9px 12px;text-align:center;font-size:11px">Status</th>'
            '</tr></thead>'
            f'<tbody>{pool_rows}</tbody></table>'
        )

        html_body = (
            '<div style="font-family:Arial,sans-serif;max-width:760px;margin:0 auto">'
            f'<div style="background:#1B2A4A;padding:20px 24px;border-bottom:3px solid #C9A84C">'
            f'<h1 style="color:white;margin:0;font-size:20px">'
            f'Bank Reconciliation &#8212; {display_date}</h1>'
            f'<p style="color:#C9A84C;margin:6px 0 0;font-size:13px">Status: '
            f'<span style="color:{status_c};background:white;padding:2px 10px;'
            f'border-radius:4px;font-weight:bold">{status_t}</span></p>'
            f'</div>'
            f'<div style="padding:20px 0">'
            f'{kpi_row}'
            f'{pool_table}'
            f'</div>'
            f'<p style="color:#888;font-size:11px;margin-top:24px;'
            f'border-top:1px solid #e0e5ed;padding-top:12px">'
            f'Generated by Keystone &#8212; GoldStandard Wealth Pvt Ltd<br>'
            f'Full reconciliation report is attached.</p></div>'
        )

        if has_breaks and is_final:
            _subject_prefix = '[EXPLAINED] '
        elif has_breaks:
            _subject_prefix = '[ACTION] '
        else:
            _subject_prefix = '[OK] '
        subject = f'{_subject_prefix}Bank Recon {display_date} &#8212; {status_t}'

        attachments = []
        if attachment_path and os.path.exists(attachment_path):
            try:
                import base64 as _b64
                with open(attachment_path, 'rb') as _f:
                    file_bytes = _f.read()
                attachments = [{
                    '@odata.type':  '#microsoft.graph.fileAttachment',
                    'name':         os.path.basename(attachment_path),
                    'contentType':  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    'contentBytes': _b64.b64encode(file_bytes).decode('utf-8'),
                }]
                logger.info(f"Attaching file: {os.path.basename(attachment_path)} ({len(file_bytes):,} bytes)")
            except Exception as e:
                logger.warning(f"Could not attach file: {e}")

        # Test-mode rewrite — diverts to a single address when configured.
        recipients, html_body = self._apply_test_mode(recipients, html_body)

        payload = {
            'message': {
                'subject': subject,
                'body': {'contentType': 'HTML', 'content': html_body},
                'toRecipients': [
                    {'emailAddress': {'address': r.strip()}}
                    for r in recipients if r.strip()
                ],
                'attachments': attachments,
            },
            'saveToSentItems': True,
        }

        url = f"{GRAPH_BASE}/users/{self.mailbox}/sendMail"
        try:
            resp = requests.post(url, headers=self._headers(),
                                 json=payload, timeout=30)
            resp.raise_for_status()
            logger.info(f"Bank recon summary email sent to: {recipients}")
            return {'ok': True,
                    'message': f"Email sent to {', '.join(recipients)}"}
        except requests.HTTPError as e:
            msg = (f"Graph API error {e.response.status_code}: "
                   f"{e.response.text[:200]}")
            logger.error(f"Bank recon email send failed: {msg}")
            return {'ok': False, 'message': msg}
        except Exception as e:
            logger.error(f"Bank recon email send failed: {e}")
            return {'ok': False, 'message': str(e)}

    # ------------------------------------------------------------------ #
    #  Archive                                                              #
    # ------------------------------------------------------------------ #

    def send_reminder_email(self, recon_type: str, date_str: str,
                            recipients: list,
                            initial_sent_at: str,
                            reminder_count: int,
                            attachment_path: str = None) -> dict:
        """Send a short reminder that the final recon email is still pending.

        Used by the background reminder worker — sends at most one short HTML
        email per invocation, re-attaching the initial report so the operator
        has everything they need to act.
        """
        if not self.is_configured():
            return {'ok': False, 'message': 'Azure credentials not configured.'}
        if not recipients:
            return {'ok': False, 'message': 'No recipients specified.'}

        from datetime import datetime as _dt
        try:
            display_date = _dt.strptime(date_str, '%Y-%m-%d').strftime('%d-%m-%Y')
        except ValueError:
            display_date = date_str

        try:
            sent_dt    = _dt.fromisoformat(initial_sent_at)
            hours_ago  = max(1, int((_dt.utcnow() - sent_dt).total_seconds() // 3600))
            sent_label = sent_dt.strftime('%d-%m-%Y %H:%M UTC')
        except Exception:
            hours_ago  = reminder_count + 1
            sent_label = initial_sent_at or 'earlier today'

        type_label = 'Holdings' if recon_type == 'holdings' else 'Bank'
        subject = (f'[REMINDER {reminder_count + 1}] {type_label} Recon '
                   f'{display_date} — final email pending')

        html_body = (
            f'<div style="font-family:Arial,sans-serif;max-width:640px;'
            f'padding:20px;color:#1B2A4A">'
            f'<h2 style="color:#B03030;margin:0 0 12px">Action Required: '
            f'{type_label} Reconciliation {display_date}</h2>'
            f'<p style="font-size:14px;line-height:1.5">The initial alert for '
            f'this reconciliation was sent <b>{hours_ago} hour(s) ago</b> '
            f'({sent_label}), but the <b>final email with break explanations</b> '
            f'has not yet been sent.</p>'
            f'<p style="font-size:14px;line-height:1.5">Please open Keystone, '
            f'review the outstanding breaks, enter explanations for amounts '
            f'above &#8377;100, and send the final email.</p>'
            f'<p style="font-size:12px;color:#666;margin-top:16px">'
            f'Reminder #{reminder_count + 1} &#8212; further reminders will '
            f'follow hourly until the final email is sent.</p>'
            f'<p style="color:#888;font-size:11px;margin-top:24px;'
            f'border-top:1px solid #e0e5ed;padding-top:12px">'
            f'Generated by Keystone &#8212; GoldStandard Wealth Pvt Ltd'
            + (f'<br>The most recent reconciliation report is attached.'
               if attachment_path else '') +
            f'</p></div>'
        )

        attachments = []
        if attachment_path and os.path.exists(attachment_path):
            try:
                import base64 as _b64
                with open(attachment_path, 'rb') as _f:
                    file_bytes = _f.read()
                attachments = [{
                    '@odata.type':  '#microsoft.graph.fileAttachment',
                    'name':         os.path.basename(attachment_path),
                    'contentType':  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    'contentBytes': _b64.b64encode(file_bytes).decode('utf-8'),
                }]
            except Exception as e:
                logger.warning(f'Reminder attach failed: {e}')

        # Test-mode rewrite — diverts to a single address when configured.
        recipients, html_body = self._apply_test_mode(recipients, html_body)

        payload = {
            'message': {
                'subject': subject,
                'body': {'contentType': 'HTML', 'content': html_body},
                'toRecipients': [
                    {'emailAddress': {'address': r.strip()}}
                    for r in recipients if r and r.strip()
                ],
                'attachments': attachments,
            },
            'saveToSentItems': True,
        }

        url = f'{GRAPH_BASE}/users/{self.mailbox}/sendMail'
        try:
            resp = requests.post(url, headers=self._headers(),
                                 json=payload, timeout=30)
            resp.raise_for_status()
            logger.info(f'Reminder #{reminder_count + 1} sent: {recon_type}/'
                        f'{date_str} → {recipients}')
            return {'ok': True, 'message': f'Reminder sent to {", ".join(recipients)}'}
        except requests.HTTPError as e:
            msg = (f'Graph API error {e.response.status_code}: '
                   f'{e.response.text[:300]}')
            logger.error(msg)
            return {'ok': False, 'message': msg}
        except Exception as e:
            logger.error(f'Reminder send failed: {e}')
            return {'ok': False, 'message': str(e)}

    def send_trade_recon_summary(self, summary_dict: dict, date_str: str,
                                 recipients: list,
                                 attachment_path: str = None,
                                 is_with_comments: bool = False) -> dict:
        """Send a Trade Reconciliation summary email via Microsoft Graph API."""
        if not self.is_configured():
            return {'ok': False, 'message': 'Azure credentials not configured.'}
        if not recipients:
            return {'ok': False, 'message': 'No recipients specified.'}

        from datetime import datetime as _dt
        display_date = _dt.strptime(date_str, '%Y-%m-%d').strftime('%d-%m-%Y')
        c1 = summary_dict.get('c1_breaks', 0)
        c2 = summary_dict.get('c2_breaks', 0)
        c3 = summary_dict.get('c3_breaks', 0)
        total_0096 = summary_dict.get('total_0096_rows', 0)
        has_breaks = (c1 + c2) > 0
        status_t = 'ACTION REQUIRED' if has_breaks else 'ALL CLEAR'
        status_c = '#B03030' if has_breaks else '#1A7A4A'
        subject_prefix = '[ACTION] ' if has_breaks else '[OK] '
        subject = f"{subject_prefix}Trade Recon {display_date}"

        def _stat(label, value, bg, fg):
            return (f'<td style="padding:12px 20px;text-align:center;background:{bg};'
                    f'border-right:1px solid #e0e5ed">'
                    f'<div style="font-size:26px;font-weight:700;color:{fg}">{value}</div>'
                    f'<div style="font-size:10px;color:{fg};text-transform:uppercase;'
                    f'letter-spacing:.07em;margin-top:2px">{label}</div></td>')

        kpi_row = (
            '<table style="border-collapse:collapse;width:100%;border:1px solid #e0e5ed;'
            'border-radius:6px;overflow:hidden;margin-bottom:20px"><tr>'
            + _stat('C1 Breaks', c1, '#fdf1f1' if c1 else '#eaf7ef', '#c0392b' if c1 else '#1a7a4a')
            + _stat('C2 Breaks', c2, '#fdf1f1' if c2 else '#eaf7ef', '#c0392b' if c2 else '#1a7a4a')
            + _stat('C3 Breaks', c3, '#fff8e6' if c3 else '#eaf7ef', '#b06820' if c3 else '#1a7a4a')
            + _stat('0096 Rows', total_0096, '#f4f6fc', '#1B2A4A')
            + '</tr></table>'
        )

        body_html = (
            f'<div style="font-family:sans-serif;max-width:700px;margin:0 auto">'
            f'<div style="background:#1B2A4A;padding:18px 24px;border-radius:6px 6px 0 0">'
            f'<span style="color:#C9A84C;font-size:18px;font-weight:700">GoldStandard</span>'
            f'<span style="color:rgba(255,255,255,.5);font-size:12px;margin-left:10px">Trade Reconciliation</span>'
            f'</div>'
            f'<div style="padding:20px 24px;background:#fff;border:1px solid #e0e5ed;border-top:none;border-radius:0 0 6px 6px">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">'
            f'<div style="font-size:15px;font-weight:700;color:#1B2A4A">{display_date}</div>'
            f'<span style="font-size:12px;font-weight:700;padding:4px 12px;border-radius:4px;'
            f'background:{"#fdf1f1" if has_breaks else "#eaf7ef"};color:{status_c}">{status_t}</span></div>'
            f'{kpi_row}'
            f'<p style="font-size:12px;color:#667;margin-top:16px">See attached Excel file for full reconciliation details.</p>'
            f'</div></div>'
        )

        # Test-mode rewrite — diverts to a single address when configured.
        recipients, body_html = self._apply_test_mode(recipients, body_html)

        to_recipients = [{'emailAddress': {'address': r}} for r in recipients]
        payload = {'message': {'subject': subject,
                               'body': {'contentType': 'HTML', 'content': body_html},
                               'toRecipients': to_recipients}}

        if attachment_path and os.path.exists(attachment_path):
            import base64
            with open(attachment_path, 'rb') as f2:
                b64 = base64.b64encode(f2.read()).decode()
            payload['message']['attachments'] = [{
                '@odata.type': '#microsoft.graph.fileAttachment',
                'name': os.path.basename(attachment_path),
                'contentBytes': b64,
                'contentType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            }]

        try:
            url  = f'{GRAPH_BASE}/users/{self.mailbox}/sendMail'
            resp = requests.post(url, headers=self._headers(), json=payload, timeout=30)
            resp.raise_for_status()
            logger.info(f'Trade recon email sent to: {recipients}')
            return {'ok': True, 'message': f'Trade recon email sent to {len(recipients)} recipient(s)'}
        except requests.HTTPError as e:
            msg = f'Graph API error {e.response.status_code}: {e.response.text[:200]}'
            logger.error(f'Trade recon email failed: {msg}')
            return {'ok': False, 'message': msg}
        except Exception as e:
            logger.error(f'Trade recon email failed: {e}')
            return {'ok': False, 'message': str(e)}


    def archive_for_date(self, date_str: str, sources: List[dict],
                         archive_base_dir: str,
                         log_callback=None,
                         since: Optional[str] = None,
                         mailbox_override: str = '') -> dict:
        """
        Archive all emails (body + attachments) from configured sender addresses
        for the lookback window around date_str.

        Versioning strategy:
          - If a file already exists in the archive, rename the OLD file with a
            timestamp suffix so the NEW file becomes the canonical name.
          - index.json per (date, source) records every version and flags
            when multiple versions of the same filename have been seen.

        Returns summary dict:
          {source_name: {'new': int, 'versioned': int, 'flagged': int}}
        """
        import json as _json
        import re as _re

        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                logger.info(msg)

        if not self.is_configured():
            log("Archive: Azure not configured — skipping")
            return {}

        # Build sender → source_name lookup from active sources
        sender_map = {}
        for src in sources:
            if not src.get('active', True):
                continue
            for addr in _normalize_senders(src):
                sender_map[addr] = src['name']

        if not sender_map:
            log("Archive: no sender addresses configured — skipping")
            return {}

        # Scope the archive pass to the same window the fetch used. If the
        # caller passed `since`, start there (minus a 30-min overlap to match
        # the fetch's jitter budget). Otherwise fall back to a 1-day lookback
        # around date_str.
        target_dt  = datetime.strptime(date_str, '%Y-%m-%d')
        dt_end     = target_dt + timedelta(days=2)
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace('Z', ''))
                dt_start = since_dt - timedelta(minutes=30)
            except Exception:
                dt_start = target_dt - timedelta(days=1)
        else:
            dt_start = target_dt - timedelta(days=1)
        dt_start_s = dt_start.strftime('%Y-%m-%dT%H:%M:%SZ')
        dt_end_s   = dt_end.strftime('%Y-%m-%dT%H:%M:%SZ')

        # Per-source mailbox override (mirrors fetch_for_date).
        mb = (mailbox_override or self.mailbox).strip()

        # Fetch messages including body (need a separate select to get body)
        first_url = (f"{GRAPH_BASE}/users/{mb}/messages"
                     f"?$filter=receivedDateTime ge {dt_start_s} "
                     f"and receivedDateTime lt {dt_end_s}"
                     f"&$select=id,subject,from,hasAttachments,"
                     f"receivedDateTime,body"
                     f"&$top=100&$orderby=receivedDateTime desc")

        messages = []
        next_url = first_url
        try:
            while next_url:
                resp = requests.get(next_url, headers=self._headers(), timeout=30)
                resp.raise_for_status()
                data = resp.json()
                messages.extend(data.get('value', []))
                next_url = data.get('@odata.nextLink')
        except Exception as e:
            log(f"Archive: fetch error — {e}")
            return {}

        log(f"Archive: processing {len(messages)} emails from {len(sender_map)} sender(s)")

        summary = {}  # source_name → {new, versioned, flagged}

        for msg in messages:
            sender  = msg.get('from', {}).get('emailAddress', {}).get('address', '').lower()
            subject = msg.get('subject', '') or ''
            msg_id  = msg['id']
            rcvd    = msg.get('receivedDateTime', '')  # e.g. "2026-03-18T02:42:22Z"

            # Match to source by sender address
            source_name = None
            for addr, sname in sender_map.items():
                if addr in sender:
                    source_name = sname
                    break
            if not source_name:
                continue

            if source_name not in summary:
                summary[source_name] = {'new': 0, 'versioned': 0, 'flagged': 0}

            # Derive the calendar date from received time (for folder routing)
            rcvd_date = rcvd[:10] if rcvd else date_str

            # Archive dir: archive/YYYY-MM-DD/source_name/
            arch_dir      = Path(archive_base_dir) / rcvd_date / source_name
            att_dir       = arch_dir / 'attachments'
            email_dir     = arch_dir / 'emails'
            att_dir.mkdir(parents=True, exist_ok=True)
            email_dir.mkdir(parents=True, exist_ok=True)

            # Load or create index
            index_path = arch_dir / 'index.json'
            try:
                index = _json.loads(index_path.read_text()) if index_path.exists() else {}
            except Exception:
                index = {}

            # ── Save email body as HTML ──────────────────────────────── #
            body_content = (msg.get('body') or {}).get('content', '')
            if body_content:
                # Slug the subject into a safe filename
                slug = _re.sub(r'[^\w\-]', '_', subject[:60]).strip('_') or 'email'
                slug = _re.sub(r'_+', '_', slug)
                html_fname = f"{rcvd[:10]}_{slug}.html"
                html_path  = email_dir / html_fname

                if not html_path.exists():
                    html_path.write_text(body_content, encoding='utf-8')
                    summary[source_name]['new'] += 1

            # ── Save attachments ─────────────────────────────────────── #
            if not msg.get('hasAttachments'):
                continue

            att_url = f"{GRAPH_BASE}/users/{mb}/messages/{msg_id}/attachments"
            try:
                att_resp = requests.get(att_url, headers=self._headers(), timeout=30)
                att_resp.raise_for_status()
                attachments = att_resp.json().get('value', [])
            except Exception as e:
                log(f"  Archive: could not get attachments for msg {msg_id[:8]}: {e}")
                continue

            for att in attachments:
                fname = att.get('name', '')
                if not fname:
                    continue

                # Get bytes
                content_bytes = att.get('contentBytes')
                if content_bytes:
                    import base64 as _b64
                    att_bytes = _b64.b64decode(content_bytes)
                else:
                    try:
                        att_id  = att['id']
                        dl_resp = requests.get(
                            f"{att_url}/{att_id}/$value",
                            headers=self._headers(), timeout=60
                        )
                        dl_resp.raise_for_status()
                        att_bytes = dl_resp.content
                    except Exception as e:
                        log(f"  Archive: download failed for {fname}: {e}")
                        continue

                canonical = att_dir / fname
                ts_suffix = rcvd.replace(':', '').replace('-', '')[:15]  # 20260318T024222

                # Version management
                if canonical.exists():
                    # Read existing file and compare bytes — skip if identical
                    try:
                        if canonical.read_bytes() == att_bytes:
                            continue  # exact duplicate — nothing to do
                    except Exception:
                        pass

                    # Different content — rename existing to versioned name
                    stem, ext  = os.path.splitext(fname)
                    versioned  = att_dir / f"{stem}.{ts_suffix}{ext}"
                    # Don't overwrite an existing versioned file either
                    counter = 0
                    while versioned.exists():
                        counter += 1
                        versioned = att_dir / f"{stem}.{ts_suffix}_{counter}{ext}"
                    try:
                        canonical.rename(versioned)
                    except Exception as e:
                        log(f"  Archive: could not version {fname}: {e}")
                        continue

                    summary[source_name]['versioned'] += 1

                    # Update index
                    entry = index.get(fname, {'versions': 1, 'flagged': False,
                                               'received': []})
                    entry['versions'] = entry.get('versions', 1) + 1
                    entry['flagged']  = True
                    entry['received'] = entry.get('received', []) + [rcvd]
                    index[fname]      = entry
                    summary[source_name]['flagged'] += 1
                    log(f"  Archive: versioned {fname} → {versioned.name}")
                else:
                    # New file — just record it
                    entry = index.get(fname, {'versions': 0, 'flagged': False,
                                               'received': []})
                    entry['versions'] = entry.get('versions', 0) + 1
                    entry['received'] = entry.get('received', []) + [rcvd]
                    index[fname]      = entry
                    summary[source_name]['new'] += 1

                # Write canonical file
                try:
                    canonical.write_bytes(att_bytes)
                except Exception as e:
                    log(f"  Archive: write failed for {fname}: {e}")
                    continue

                # Persist index
                try:
                    index_path.write_text(_json.dumps(index, indent=2))
                except Exception as e:
                    log(f"  Archive: index write failed: {e}")

        # Log summary
        total_new = sum(v['new'] for v in summary.values())
        total_ver = sum(v['versioned'] for v in summary.values())
        total_flg = sum(v['flagged'] for v in summary.values())
        if summary:
            log(f"Archive complete — {total_new} new, {total_ver} versioned"
                + (f", {total_flg} flagged" if total_flg else ""))
        else:
            log("Archive: no matching emails found")

        return summary

    def send_explanation_followup(self,
                                  recon_type: str,
                                  date_str: str,
                                  recipients: list,
                                  explanations: list,
                                  explainer_email: str = '',
                                  accepted_all: bool = False,
                                  attachment_path: str = None) -> dict:
        """Send the post-explanation follow-up email.

        Called by the _cmd_send_final_email agent handler after an
        operator submits per-break explanations (or accept-all) via
        the control plane's explain page. Builds a minimal HTML body
        listing every explanation + the explainer's email + an
        accept-all indicator, and attaches the existing Excel report
        (if still on disk).

        Separate from send_recon_summary / send_bank_recon_summary
        because those methods expect the full in-memory results /
        summary dict at recon time; by the time we're following up,
        the process has long exited and all we have is the sidecar.
        This method only needs the explanations payload (which came
        from the control plane) and the on-disk Excel path.

        Args:
            recon_type:       "bank" / "holdings" (trade doesn't use)
            date_str:         YYYY-MM-DD
            recipients:       list of email addresses
            explanations:     list of {break_id, explanation, explained_amount}
            explainer_email:  email of the operator who explained
            accepted_all:     True if the operator hit "Accept all"
            attachment_path:  absolute path to the Keystone_* Excel

        Returns dict {ok, message}.
        """
        if not self.is_configured():
            return {'ok': False, 'message': 'Azure credentials not configured.'}
        if not recipients:
            return {'ok': False, 'message': 'No recipients specified.'}

        from datetime import datetime as _dt
        try:
            display_date = _dt.strptime(date_str, '%Y-%m-%d').strftime('%d-%m-%Y')
        except ValueError:
            display_date = date_str

        type_label = recon_type.capitalize()
        subject_prefix = '[EXPLAINED]' if accepted_all else '[UPDATE]'
        subject = f"{subject_prefix} {type_label} Recon {display_date}"

        # Build the explanations list as HTML rows.
        rows_html = ''
        for exp in (explanations or []):
            bid = str(exp.get('break_id') or '')
            text = str(exp.get('explanation') or '').replace('<', '&lt;').replace('>', '&gt;')
            amt = exp.get('explained_amount')
            amt_cell = ''
            if amt is not None and str(amt).strip():
                try:
                    amt_val = float(amt)
                    amt_cell = f'<td style="padding:6px 10px;font-family:monospace;text-align:right;border:1px solid #e0e0e0">₹{amt_val:,.2f}</td>'
                except (TypeError, ValueError):
                    amt_cell = f'<td style="padding:6px 10px;border:1px solid #e0e0e0">{amt}</td>'
            else:
                amt_cell = '<td style="padding:6px 10px;color:#999;border:1px solid #e0e0e0">—</td>'
            rows_html += (
                f'<tr>'
                f'<td style="padding:6px 10px;font-family:monospace;border:1px solid #e0e0e0">{bid}</td>'
                f'<td style="padding:6px 10px;border:1px solid #e0e0e0">{text}</td>'
                f'{amt_cell}'
                f'</tr>'
            )

        explainer_line = (
            f'<p style="margin:16px 0 8px;color:#555">'
            f'Explained by <strong>{explainer_email}</strong></p>'
            if explainer_email else ''
        )
        accept_note = (
            '<p style="color:#1A7A4A;font-weight:600;margin:12px 0">'
            '&#x2713; All breaks have been explained or are within tolerance. '
            'The operator has accepted the run.</p>'
            if accepted_all else ''
        )

        if rows_html:
            table_html = (
                '<table style="border-collapse:collapse;width:100%;'
                'font-family:Arial,sans-serif;font-size:13px;margin:12px 0">'
                '<thead><tr style="background:#1B2A4A;color:white">'
                '<th style="padding:8px 10px;text-align:left">Break</th>'
                '<th style="padding:8px 10px;text-align:left">Explanation</th>'
                '<th style="padding:8px 10px;text-align:right">Amount</th>'
                '</tr></thead><tbody>' + rows_html + '</tbody></table>'
            )
        else:
            table_html = (
                '<p style="color:#666;margin:12px 0">'
                'No per-break explanations were submitted. '
                '(All breaks were within tolerance.)</p>'
            )

        body_html = (
            '<div style="font-family:Arial,sans-serif;max-width:720px;'
            'padding:20px;color:#1B2A4A">'
            f'<h2 style="color:#AC8A2F;margin:0 0 12px">'
            f'{type_label} Reconciliation &mdash; {display_date} &mdash; {subject_prefix}'
            '</h2>'
            f'{explainer_line}'
            f'{accept_note}'
            f'{table_html}'
            '<p style="color:#666;font-size:12px;margin-top:16px">'
            'The updated reconciliation report is attached. This follow-up '
            'closes the open break window &mdash; no further hourly reminders will fire.'
            '</p>'
            '<p style="color:#888;font-size:11px;margin-top:20px">'
            'Generated by Keystone &mdash; GoldStandard Wealth Pvt Ltd'
            '</p>'
            '</div>'
        )

        # Build attachments
        import base64 as _b64, os as _os
        attachments = []
        if attachment_path and _os.path.exists(attachment_path):
            try:
                with open(attachment_path, 'rb') as _f:
                    file_bytes = _f.read()
                attachments = [{
                    '@odata.type':  '#microsoft.graph.fileAttachment',
                    'name':         _os.path.basename(attachment_path),
                    'contentType':  'application/octet-stream',
                    'contentBytes': _b64.b64encode(file_bytes).decode('ascii'),
                }]
            except Exception as e:
                logger.warning(f'send_explanation_followup: attach failed: {e}')

        token = self._get_token()
        mailbox = self.mailbox
        import requests as _req
        try:
            r = _req.post(
                f'https://graph.microsoft.com/v1.0/users/{mailbox}/sendMail',
                headers={'Authorization': f'Bearer {token}',
                         'Content-Type': 'application/json'},
                json={
                    'message': {
                        'subject': subject,
                        'body': {'contentType': 'HTML', 'content': body_html},
                        'toRecipients': [
                            {'emailAddress': {'address': r}} for r in recipients
                        ],
                        'attachments': attachments,
                    },
                    'saveToSentItems': True,
                },
                timeout=30,
            )
        except Exception as e:  # noqa: BLE001
            return {'ok': False, 'message': f'network error: {e}'}
        if r.status_code not in (200, 202):
            return {
                'ok':      False,
                'message': f'Graph sendMail {r.status_code}: {r.text[:300]}',
            }
        return {
            'ok':      True,
            'message': f'sent to {len(recipients)} recipient(s)',
        }
