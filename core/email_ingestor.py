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

# Branding (operator-overridable via config/azure.json:branding) is
# imported lazily so a missing module on a partial deploy doesn't take
# the whole ingestor down.
def _branding_get() -> dict:
    try:
        from core.branding import get as _b
        return _b()
    except Exception:
        return {'firm_name':       'GoldStandard Wealth Pvt Ltd',
                 'firm_short_name': 'GoldStandard'}

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


def _normalize_zip_prefixes(source: dict) -> list:
    """Return zip_name_prefix values as a list (stripped, case-preserved).

    Accepts either a string (legacy) or a list — parallel to sender_email.
    Custodians sometimes rename their attachments (e.g. ICICI now sends
    both "End_Client_Holding_GOLDWLTH_*.zip" and
    "PMS_Holding_ISIN_Wise_GOLDWLTH_*.zip"), so an attachment is accepted
    if its name starts with ANY of the listed prefixes.

    Empty / missing → empty list (no prefix filtering).
    """
    raw = source.get('zip_name_prefix')
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if x and str(x).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def _received_stamp(rcvd: str) -> str:
    """Return the email's received time as YYYYMMDDTHHMMSS, or '' if not
    parseable. Used to tag saved filenames so a re-sent / rectified file
    can be distinguished from the original on disk."""
    from datetime import datetime as _dt
    if rcvd and len(rcvd) >= 19:
        try:
            return _dt.strptime(rcvd[:19], '%Y-%m-%dT%H:%M:%S').strftime('%Y%m%dT%H%M%S')
        except ValueError:
            pass
    return ''


def _stamp_filename(original_name: str, rcvd: str,
                    fallback_date: str = '') -> tuple:
    """Return (save_name, dedup_key) for an attachment.

    save_name  — original name with '_YYYYMMDDTHHMMSS' inserted before the
                 extension. If the received time can't be parsed, fall back
                 to '_YYYYMMDD' so we still get a stamp.
    dedup_key  — original stem + extension (no timestamp). Used to detect
                 "same logical file re-sent later" so newest-wins dedup
                 works regardless of how many custodian-embedded timestamps
                 a filename already carries.
    """
    from pathlib import Path as _P
    p    = _P(original_name)
    stem = p.stem
    sfx  = p.suffix
    ts   = _received_stamp(rcvd)
    if not ts:
        ts = (fallback_date or '').replace('-', '') or ''
    save_name = f"{stem}_{ts}{sfx}" if ts else original_name
    dedup_key = f"{stem}{sfx}"
    return save_name, dedup_key


class EmailIngestor:

    def __init__(self, azure_config: dict):
        # Env vars override azure.json — same pattern as source secrets.
        # Lets us keep the M365 client_secret out of azure.json on Azure
        # while preserving local-dev convenience.
        self.tenant_id     = (os.environ.get('KEYSTONE_AZURE_TENANT_ID', '').strip()
                              or azure_config.get('tenant_id', '').strip())
        self.client_id     = (os.environ.get('KEYSTONE_AZURE_CLIENT_ID', '').strip()
                              or azure_config.get('client_id', '').strip())
        self.client_secret = (os.environ.get('KEYSTONE_AZURE_CLIENT_SECRET', '').strip()
                              or azure_config.get('client_secret', '').strip())
        self.mailbox       = azure_config.get('mailbox', '').strip()
        # Operator-set expiry date for client_secret (ISO YYYY-MM-DD).
        # Azure AD secrets expire (default 6/12/24 months); when the
        # operator rotates a secret they record its expiry here and
        # _get_token() warns as the date approaches.
        self.client_secret_expires_on = (
            os.environ.get('KEYSTONE_AZURE_CLIENT_SECRET_EXPIRES_ON', '').strip()
            or str(azure_config.get('client_secret_expires_on') or '').strip()
        )
        # Test mode: when set to a non-empty email address, every
        # outgoing message has its recipients rewritten to this single
        # address and a banner prepended to the body listing the
        # originals. Used for troubleshooting without spamming real
        # operations distribution lists.
        self.test_mode_email = str(azure_config.get('test_mode_email') or '').strip()
        self._token        = None
        self._token_expiry = None
        self._expiry_warned_for: Optional[str] = None  # date we last warned about

    def is_configured(self) -> bool:
        return all([self.tenant_id, self.client_id,
                    self.client_secret, self.mailbox])

    def configured_diagnostic(self) -> str:
        """Return a one-line diagnostic listing which Azure config fields
        are missing. Used by callers that log 'Azure not configured' so
        the operator can see WHICH field tripped the check rather than
        guessing across four possibilities (tenant_id, client_id,
        client_secret, mailbox). Returns empty string when fully
        configured."""
        missing = []
        if not self.tenant_id:     missing.append('tenant_id')
        if not self.client_id:     missing.append('client_id')
        if not self.client_secret: missing.append('client_secret')
        if not self.mailbox:       missing.append('mailbox')
        return f"missing: {', '.join(missing)}" if missing else ''

    def _log_send_intent(self, kind: str, subject: str,
                          to_list: list, bcc_list: list) -> None:
        """One-line operator-readable diagnostic before each Graph send.

        Captures the four things that determine whether a message
        actually lands in the right inboxes:
          - sender mailbox (the From address)
          - test-mode override (when set, all sends go to a single
            override address — easy to forget about and silently
            redirects every email)
          - resolved TO list (after test_mode + self-bcc transforms)
          - resolved BCC list (after self-bcc split)

        Surfaces as a single INFO line per send in Azure App Service
        Log Stream, so when an operator reports "X didn't get the mail"
        we can confirm in seconds whether (a) X was on the list at all,
        (b) X was diverted by test mode, or (c) the message left the
        app and the issue is downstream (Junk filter, inbox rule,
        mailbox quota, message trace).
        """
        tm = f' TEST_MODE={self.test_mode_email!r}' if self.test_mode_email else ''
        logger.info(
            f"send[{kind}] from={self.mailbox!r} subject={subject[:80]!r}"
            f"{tm} to={to_list} bcc={bcc_list}"
        )

    def _split_self_to_bcc(self, recipients: list) -> tuple:
        """Split a recipient list so any address matching the sending
        mailbox (``self.mailbox``) lands in BCC instead of TO.

        Microsoft 365 / Exchange transport runs an anti-loop check on
        ``sendMail`` calls: when the From mailbox is also in TO/CC, the
        transport suppresses the Inbox delivery and the message ends up
        in Sent Items only. That's why an operator who has the sending
        address (e.g. operations@thegoldstandard.in) listed in
        recon_recipients sees other recipients receive the email but
        not their own Inbox. BCC self-send is treated more permissively
        by the same transport, so moving the self-match to BCC restores
        Inbox delivery without changing the visible TO line for the
        other recipients.

        Returns ``(to_recipients, bcc_recipients)``. Both lists are
        deduplicated and empty-stripped. Logs a one-line warning when
        the split actually moves anything so the audit trail shows
        why operator's Inbox got the mail via BCC.
        """
        own = (self.mailbox or '').strip().lower()
        to_list: list[str] = []
        bcc_list: list[str] = []
        seen_to: set = set()
        seen_bcc: set = set()
        for r in (recipients or []):
            addr = (r or '').strip()
            if not addr:
                continue
            low = addr.lower()
            if own and low == own:
                if low not in seen_bcc:
                    seen_bcc.add(low)
                    bcc_list.append(addr)
            else:
                if low not in seen_to:
                    seen_to.add(low)
                    to_list.append(addr)
        if bcc_list:
            logger.info(
                f"send: moved sender-mailbox recipient(s) {bcc_list} to BCC "
                f"(Exchange anti-loop suppresses Inbox delivery on TO/CC self-send)")
        return to_list, bcc_list

    def _apply_test_mode(self, recipients: list, body_html: str) -> tuple:
        """Rewrite recipients + body when test mode is on.

        Returns (recipients, body_html) unchanged when test_mode_email is
        empty. When set, every outgoing email is diverted to that single
        address with a yellow banner annotating the original recipient list.
        """
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
        """Get or refresh OAuth2 access token using client credentials.

        Side-effects on each call:
          - If `client_secret_expires_on` is set, log a WARNING/ERROR
            when the secret is within 30 / 7 days of expiry (once per
            day per process, not per call).
          - On token failure, detect AADSTS7000222 (client_secret
            expired) or AADSTS7000215 (invalid secret) and re-raise
            with a clear, actionable message instead of the opaque
            HTTPError text.
        """
        # Pre-flight: warn if secret_expires_on is approaching
        self._check_secret_expiry()

        now = datetime.utcnow()
        if self._token and self._token_expiry and now < self._token_expiry:
            return self._token

        url = TOKEN_URL.format(tenant_id=self.tenant_id)
        try:
            resp = requests.post(url, data={
                'grant_type':    'client_credentials',
                'client_id':     self.client_id,
                'client_secret': self.client_secret,
                'scope':         GRAPH_SCOPE,
            }, timeout=30)
            resp.raise_for_status()
        except requests.HTTPError as e:
            body = ''
            try:
                body = e.response.text or ''
            except Exception:
                pass
            if 'AADSTS7000222' in body:
                raise RuntimeError(
                    "M365 client_secret is EXPIRED. The email ingestor and "
                    "all outbound notification emails will fail until a new "
                    "secret is generated in Azure Portal -> App Registrations "
                    "and saved via Settings -> Azure Config. Original error: "
                    + body[:300]
                ) from e
            if 'AADSTS7000215' in body:
                raise RuntimeError(
                    "M365 client_secret is INVALID (wrong value). Verify the "
                    "secret in Settings -> Azure Config matches the one in "
                    "the Azure App Registration. Original error: " + body[:300]
                ) from e
            raise

        data = resp.json()
        self._token = data['access_token']
        self._token_expiry = now + timedelta(seconds=data.get('expires_in', 3600) - 60)
        return self._token

    def _check_secret_expiry(self) -> None:
        """Log days-remaining on the M365 client_secret when getting close.

        Quiet when expires_on is unset (operator never recorded it).
        Warns once per process per UTC date — repeat warnings would
        spam the log on every email fetch.
        """
        if not self.client_secret_expires_on:
            return
        try:
            exp = datetime.strptime(self.client_secret_expires_on[:10], '%Y-%m-%d')
        except (ValueError, TypeError):
            return  # Bad format — silent, operator will see "unset" in UI

        today = datetime.utcnow().date()
        days_left = (exp.date() - today).days
        today_iso = today.isoformat()
        if self._expiry_warned_for == today_iso:
            return  # Already warned today

        if days_left < 0:
            logger.error(
                f"M365 client_secret EXPIRED {-days_left} day(s) ago "
                f"({self.client_secret_expires_on}). Token requests will "
                f"fail. Rotate the secret in Azure Portal."
            )
            self._expiry_warned_for = today_iso
        elif days_left <= 7:
            logger.error(
                f"M365 client_secret expires in {days_left} day(s) "
                f"({self.client_secret_expires_on}). Rotate now to avoid "
                f"an email ingest outage."
            )
            self._expiry_warned_for = today_iso
        elif days_left <= 30:
            logger.warning(
                f"M365 client_secret expires in {days_left} day(s) "
                f"({self.client_secret_expires_on}). Plan a rotation."
            )
            self._expiry_warned_for = today_iso

    def secret_expiry_status(self) -> dict:
        """Return {'configured': bool, 'expires_on': str, 'days_left': int|None,
                   'severity': 'ok'|'warn'|'critical'|'expired'|'unset'}.

        Used by /api/auth-me etc. so the UI can render a banner without
        having to recompute the math itself.
        """
        if not self.client_secret_expires_on:
            return {'configured': False, 'expires_on': '',
                    'days_left': None, 'severity': 'unset'}
        try:
            exp = datetime.strptime(self.client_secret_expires_on[:10], '%Y-%m-%d')
        except (ValueError, TypeError):
            return {'configured': False, 'expires_on': self.client_secret_expires_on,
                    'days_left': None, 'severity': 'unset'}
        today = datetime.utcnow().date()
        days_left = (exp.date() - today).days
        if days_left < 0:
            sev = 'expired'
        elif days_left <= 7:
            sev = 'critical'
        elif days_left <= 30:
            sev = 'warn'
        else:
            sev = 'ok'
        return {'configured': True,
                'expires_on': self.client_secret_expires_on,
                'days_left': days_left,
                'severity': sev}

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
                                self.mailbox (default operations@…). Used by
                                the _run_email_fetch grouper to fan out one
                                Graph call per (mailbox, sources_subset)
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

        # Per-source mailbox override.  Most sources land in the default
        # operations@thegoldstandard.in inbox; Value Research sends to
        # vr@thegoldstandard.in.  When the caller supplies a non-empty
        # mailbox_override, every Graph URL in this call uses it.
        mb = (mailbox_override or self.mailbox).strip()

        log(f"Fetching emails from {mb} — window {dt_start_s} to {dt_end_s}")
        log(f"Matching by sender address and zip filename prefix only")

        # Fetch all messages in the lookback window using pagination
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
        log(f"Found {len(messages)} email(s) in lookback window across {page} page(s) "
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
            att_url = f"{GRAPH_BASE}/users/{mb}/messages/{msg_id}/attachments"
            if att_err is not None:
                results.append({'source': source_name, 'status': 'error',
                                 'message': f"Failed to get attachments: {att_err}"})
                continue

            # Diagnostic: log every attachment we see on matched emails so
            # dropped files aren't invisible.
            _att_summary = [f"{a.get('name','?')} ({a.get('@odata.type','?').split('.')[-1]})"
                             for a in attachments]
            log(f"    Attachments ({len(attachments)}): {_att_summary}")

            for att in attachments:
                att_name = att.get('name', '')
                is_bank_source    = matched_source.get('is_bank', False)
                attachment_type   = matched_source.get('attachment_type', 'zip')

                # 'direct' type: accept PDF, XLS, XLSX, CSV directly (no zip needed)
                if attachment_type == 'direct':
                    ext = att_name.lower().rsplit('.', 1)[-1] if '.' in att_name else ''
                    if ext not in ('pdf', 'xls', 'xlsx', 'csv', 'zip'):
                        log(f"    Skipping attachment (bad ext '{ext}'): {att_name}")
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

                # Check zip name prefix if configured (for zip attachments).
                # zip_name_prefix may be a string (legacy) or a list of
                # acceptable prefixes. Match if the filename starts with ANY
                # of them.
                zip_prefixes = _normalize_zip_prefixes(matched_source)
                if zip_prefixes and not any(
                    att_name.startswith(p) for p in zip_prefixes
                ):
                    log(f"    Skipping attachment (prefix mismatch): {att_name}"
                        f" (expected start with: {zip_prefixes})")
                    continue

                # Check file prefix if configured (for direct attachments)
                file_prefix = matched_source.get('file_prefix', '')
                if file_prefix and not att_name.lower().startswith(file_prefix.lower()):
                    log(f"    Skipping attachment (file_prefix mismatch): {att_name} "
                        f"(expected startswith: {file_prefix!r})")
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
                    log(f"    Skipping attachment (file_contains mismatch): {att_name} "
                        f"(expected any of: {file_contains})")
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

                # For Kotak: determine per-strategy subfolder from zip filename.
                # Kotak zips always start with "GOLDSTANDARD WEALTH PVT LTD "
                # followed by the strategy name. Examples observed:
                #   "GOLDSTANDARD WEALTH PVT LTD MYSTIC WEVA.zip"   → kotak_MYSTIC_WEVA
                #   "GOLDSTANDARD WEALTH PVT LTD MYSTIC WEMO.zip"   → kotak_MYSTIC_WEMO
                #   "GOLDSTANDARD WEALTH PVT LTD ER INDIA50.zip"    → kotak_ER_INDIA50
                #   "GOLDSTANDARD WEALTH PVT LTD CAUTILYA TC.zip"   → kotak_CAUTILYA_TC
                # Future strategies are handled automatically — we extract
                # whatever comes after the common prefix.
                # Applies to both source='kotak' (holdings) and 'kotak_bank'.
                import re as _re
                if source_name in ('kotak', 'kotak_bank'):
                    fname_norm = att_name.upper().replace(' ', '_')
                    # Strip the file extension before matching so ".ZIP" isn't
                    # part of the strategy suffix.
                    stem_norm = _re.sub(r'\.(ZIP|XLSX|XLS|CSV)$', '', fname_norm)
                    m = _re.match(r'GOLDSTANDARD_WEALTH_PVT_LTD_(.+)$', stem_norm)
                    if m:
                        strategy_suffix = m.group(1)
                        # Holdings: kotak_MYSTIC_WEVA | Bank: kotak_bank_MYSTIC_WEVA
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
                        # minus file_date_offset. This determines the target
                        # folder; the filename is tagged with the full received
                        # timestamp by _stamp_filename() so rectifications can
                        # be distinguished from originals.
                        from datetime import datetime as _dt, timedelta as _td
                        _rcvd_dtfull = None
                        if rcvd and len(rcvd) >= 19:
                            try:
                                _rcvd_dtfull = _dt.strptime(
                                    rcvd[:19], '%Y-%m-%dT%H:%M:%S')
                            except ValueError:
                                _rcvd_dtfull = None
                        _rcvd_dt = _rcvd_dtfull.date() if _rcvd_dtfull else None
                        _eff_dt  = _rcvd_dt
                        if _rcvd_dt and offset > 0:
                            _eff_dt = _rcvd_dt - _td(days=offset)
                        _folder_date = _eff_dt.strftime('%Y-%m-%d') if _eff_dt else date_str
                        save_name, _stem_key = _stamp_filename(att_name, rcvd, _folder_date)

                        final_dir = str(file_manager.raw_dir(_folder_date, dest_folder))
                        Path(final_dir).mkdir(parents=True, exist_ok=True)
                        final_path = os.path.join(final_dir, save_name)
                        # Dedup key — strip the timestamp tag so two emails
                        # with different received times but the SAME logical
                        # filename (e.g. a rectification of the same CN) are
                        # recognised as the same document. Messages are
                        # processed newest-first, so the FIRST save wins.
                        dedup_key = f"{dest_folder}/{_stem_key}"
                        if dedup_key in _saved_this_run:
                            log(f"    Skipping older duplicate: {save_name} "
                                f"(newer version already saved for {_stem_key})")
                            continue
                        if os.path.exists(final_path):
                            log(f"    Overwriting (re-fetch): {save_name}")
                        _shutil.move(zip_stage_path, final_path)
                        _saved_this_run.add(dedup_key)
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
                        save_name, stem_key = _stamp_filename(att_name, rcvd, holding_date)
                        dedup_key = f"{dest_folder}/{stem_key}"
                        if dedup_key in _saved_this_run:
                            log(f"    Skipping older duplicate: {save_name} "
                                f"(newer version already saved for {stem_key})")
                            continue
                        final_dir = str(file_manager.raw_dir(holding_date, dest_folder))
                        Path(final_dir).mkdir(parents=True, exist_ok=True)
                        final_path = os.path.join(final_dir, save_name)
                        if os.path.exists(final_path):
                            log(f"    Skipping (already have newer): {save_name}")
                            continue
                        _shutil.move(zip_stage_path, final_path)
                        _saved_this_run.add(dedup_key)
                        results.append({
                            'source':       dest_folder,
                            'holding_date': holding_date,
                            'status':       'ok',
                            'zip':          save_name,
                            'files':        [save_name],
                            'message':      f'Saved AES-256 zip → {holding_date}/{dest_folder}/{save_name}',
                        })
                        log(f"    → {holding_date}/{dest_folder}/{save_name} (AES-256, saved for 7z parsing)")
                        continue

                    # Plain text/CSV attachments (ICICI bank) — save directly, no extraction.
                    if attachment_type in ('txt', 'txt_or_csv'):
                        holding_date = self._holding_date_from_filename(
                            att_name, date_str, offset, received_date=rcvd,
                            is_bank=is_bank_source
                        )
                        save_name, stem_key = _stamp_filename(att_name, rcvd, holding_date)
                        dedup_key = f"{dest_folder}/{stem_key}"
                        if dedup_key in _saved_this_run:
                            log(f"    Skipping older duplicate: {save_name} "
                                f"(newer version already saved for {stem_key})")
                            continue
                        final_dir = str(file_manager.raw_dir(holding_date, dest_folder))
                        Path(final_dir).mkdir(parents=True, exist_ok=True)
                        final_path = os.path.join(final_dir, save_name)
                        if os.path.exists(final_path):
                            log(f"    Skipping (already have newer): {save_name}")
                            continue
                        _shutil.move(zip_stage_path, final_path)
                        _saved_this_run.add(dedup_key)
                        results.append({
                            'source':       dest_folder,
                            'holding_date': holding_date,
                            'status':       'ok',
                            'files':        [save_name],
                            'message':      f'Saved TXT → {holding_date}/{dest_folder}/{save_name}',
                        })
                        log(f"    → {holding_date}/{dest_folder}/{save_name}")
                        continue

                    # Standard zip: extract with Python zipfile, route each file
                    from core.secret_resolver import resolve_from_source_dict
                    zip_password = resolve_from_source_dict(matched_source, 'zip_password')
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
                        # Exception 1: Kotak zips contain both XLSX (holdings) and CSV
                        # (bank statement) — keep CSVs so bank recon can find them.
                        # Exception 2: sources with keep_csv_in_zip=true (e.g. Vidal
                        # EoD source) — the zip is purpose-built around its CSV.
                        is_bank_source = is_bank_source_flag
                        is_kotak = source_name in ('kotak', 'kotak_bank')
                        keep_csv = matched_source.get('keep_csv_in_zip', False)
                        if (not is_bank_source and not is_kotak and not keep_csv
                                and fname.lower().endswith(('.zip', '.csv'))):
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
                                is_bank=is_bank_source_flag,
                                filename_date_format=matched_source.get(
                                    'filename_date_format', ''),
                            )
                        save_name, stem_key = _stamp_filename(fname, rcvd, holding_date)
                        dedup_key = f"{dest_folder}/{stem_key}"
                        if dedup_key in _saved_this_run:
                            log(f"    Skipping older duplicate: {save_name} "
                                f"(newer version already saved for {stem_key})")
                            continue
                        final_dir = str(file_manager.raw_dir(holding_date, dest_folder))
                        Path(final_dir).mkdir(parents=True, exist_ok=True)
                        final_path = os.path.join(final_dir, save_name)
                        # For bank sources: don't overwrite — the first-written file comes
                        # from the newest email (emails are processed newest-first).
                        # Overwriting would replace current-day data with older-day data.
                        if os.path.exists(final_path):
                            if is_bank_source:
                                log(f"    Skipping (already have newer): {save_name}")
                                continue
                            else:
                                os.remove(final_path)
                        _shutil.move(extracted_path, final_path)
                        _saved_this_run.add(dedup_key)
                        routed_files.append((save_name, holding_date))
                        log(f"    → {holding_date}/{dest_folder}/{save_name}")

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
                                    is_bank: bool = False,
                                    filename_date_format: str = '') -> str:
        """
        Derive the data date from a filename + email metadata.

        For **bank statements** (is_bank=True):
          Bank statements are always T+1 — received the day after the data date.
          1. Primary: email received date minus 1 day (most reliable)
          2. Fallback: date found in filename minus offset
          3. No further fallback — missing date should be flagged

        For **holdings / other sources** (is_bank=False):
          1. Date found in filename minus EFFECTIVE offset (see below)
          2. Fallback: email received date minus EFFECTIVE offset (if offset > 0)
          3. Final fallback: requested_date (pivot date of the fetch)

        **Effective offset rule** (non-bank only):
          The configured `offset` assumes the custodian generates the file
          *after midnight* (filename date is T+1, real data is T → subtract 1).
          ICICI End_Client_Holding sometimes makes the cutoff and is sent
          late the same evening — filename date is already the data date.
          When the email landed between 20:00 and 23:59 IST, treat as
          same-day delivery and skip the offset. Outside that window
          (typically post-midnight T+1 deliveries) keep the configured offset.
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

        # ── Effective offset (non-bank only) ──────────────────────────
        # Evening (20:00–23:59 IST) deliveries skip the offset. Custodians
        # that occasionally squeeze a same-day file out before midnight
        # stamp it with the data date directly, so subtracting `offset`
        # would push it to D-1. Widened from the original 22:00 cutoff
        # to capture earlier-evening deliveries (a few custodians push
        # files between 20:00 and 22:00 IST when the post-trade run
        # finishes early).
        effective_offset = offset
        if not is_bank and offset > 0 and received_date:
            try:
                _r = datetime.strptime(received_date[:19], '%Y-%m-%dT%H:%M:%S')
                _r_ist = _r + timedelta(hours=5, minutes=30)
                if 20 <= _r_ist.hour < 24:
                    effective_offset = 0
            except ValueError:
                pass

        # ── Date patterns in filename ─────────────────────────────────
        # Sources with `filename_date_format` in their config get a more
        # specific pattern set. Default falls through to the original
        # 8-digit-or-separated patterns. Useful for Vidal-style filenames
        # like XC240426.csv where DDMMYY=240426 (April 24, 2026) is too
        # short for the default 8-digit regex to match.
        if filename_date_format == 'DDMMYY':
            patterns = [
                # Match a 2-letter prefix + DDMMYY at start to avoid
                # picking up arbitrary 6-digit runs elsewhere in the
                # filename. Resolves to a 4-digit year as 2000 + YY.
                (r'^[A-Za-z]{2}(\d{2})(\d{2})(\d{2})\b', 'DDMMYY'),
            ]
        else:
            patterns = [
                (r'(\d{2})(\d{2})(\d{4})', '%d%m%Y'),   # DDMMYYYY e.g. 18032026
                (r'(\d{4})(\d{2})(\d{2})', '%Y%m%d'),   # YYYYMMDD e.g. 20260318
                (r'(\d{1,2})[_\-](\d{1,2})[_\-](\d{4})', None),  # D_M_YYYY / DD-MM-YYYY
            ]

        for pattern, fmt in patterns:
            for m in re.finditer(pattern, filename):
                try:
                    if fmt == 'DDMMYY':
                        g = m.groups()
                        file_dt = datetime.strptime(
                            f"{g[0]}/{g[1]}/20{g[2]}", '%d/%m/%Y'
                        )
                    elif fmt:
                        file_dt = datetime.strptime(m.group(0), fmt)
                    else:
                        g = m.groups()
                        file_dt = datetime.strptime(
                            f"{g[0].zfill(2)}/{g[1].zfill(2)}/{g[2]}", '%d/%m/%Y'
                        )
                    req_dt = datetime.strptime(requested_date, '%Y-%m-%d')
                    if abs((file_dt - req_dt).days) <= 7:
                        holding_dt = file_dt - timedelta(days=effective_offset)
                        return holding_dt.strftime('%Y-%m-%d')
                except (ValueError, IndexError):
                    continue

        # ── Non-bank fallback: received date with effective offset ───
        if not is_bank and offset and received_date:
            try:
                rcvd_dt = datetime.strptime(received_date[:19], '%Y-%m-%dT%H:%M:%S')
                rcvd_ist = rcvd_dt + timedelta(hours=5, minutes=30)
                return (rcvd_ist - timedelta(days=effective_offset)).strftime('%Y-%m-%d')
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


    def send_simple_email(self, subject: str, body_html: str,
                          recipients: list,
                          attachments: list = None,
                          cc: list = None) -> dict:
        """Generic Microsoft Graph sendMail. attachments is a list of
        {'name': str, 'path': str, 'content_type': str (optional)} dicts;
        each gets base64-encoded and attached. ``cc`` is an optional
        list of additional addresses to copy — used by the welcome-
        email flow to copy fund-manager + intermediary on each pool's
        client mail. Used by ad-hoc flows like the new-pool broker
        invitation email — keeps the heavyweight send_*_summary
        methods specialised to recon results."""
        import base64 as _b64
        import os as _os
        if not self.is_configured():
            return {'ok': False, 'message': 'Azure credentials not configured.'}
        if not recipients:
            return {'ok': False, 'message': 'No recipients specified.'}
        att_payload = []
        for a in (attachments or []):
            p = a.get('path')
            if not p or not _os.path.exists(p):
                logger.warning(f"send_simple_email: attachment missing — {p}")
                continue
            try:
                with open(p, 'rb') as fh:
                    raw = fh.read()
                att_payload.append({
                    '@odata.type':  '#microsoft.graph.fileAttachment',
                    'name':         a.get('name') or _os.path.basename(p),
                    'contentType':  a.get('content_type') or 'application/octet-stream',
                    'contentBytes': _b64.b64encode(raw).decode('utf-8'),
                })
            except Exception as e:
                logger.warning(f"send_simple_email: failed to attach {p}: {e}")
        recipients, body_html = self._apply_test_mode(recipients, body_html)
        to_list, bcc_list = self._split_self_to_bcc(recipients)
        self._log_send_intent('simple', subject, to_list, bcc_list)
        # Cc list — dedupe against to_list / bcc_list so the same
        # address never appears twice on the message envelope.
        cc_list = []
        if cc:
            seen = {a.lower() for a in (to_list + bcc_list)}
            for addr in cc:
                a = (addr or '').strip()
                if a and a.lower() not in seen:
                    cc_list.append(a)
                    seen.add(a.lower())
        msg = {
            'subject': subject,
            'body':    {'contentType': 'HTML', 'content': body_html},
            'toRecipients': [{'emailAddress': {'address': r}} for r in to_list],
            'attachments': att_payload,
        }
        if cc_list:
            msg['ccRecipients'] = [{'emailAddress': {'address': r}} for r in cc_list]
        if bcc_list:
            msg['bccRecipients'] = [{'emailAddress': {'address': r}} for r in bcc_list]
        payload = {'message': msg, 'saveToSentItems': True}
        url = f"{GRAPH_BASE}/users/{self.mailbox}/sendMail"
        try:
            resp = requests.post(url, headers=self._headers(),
                                 json=payload, timeout=60)
            resp.raise_for_status()
            sent_to = to_list + (bcc_list and [f'{a} (bcc)' for a in bcc_list] or [])
            logger.info(f"send[simple] graph_status={resp.status_code} OK")
            return {'ok': True,
                    'message': f"Sent to {', '.join(sent_to)} "
                               f"({len(att_payload)} attachment(s))"}
        except requests.HTTPError as e:
            msg = f"Graph error {e.response.status_code}: {e.response.text[:300]}"
            logger.error(msg)
            return {'ok': False, 'message': msg}
        except Exception as e:
            logger.error(f"send_simple_email failed: {e}")
            return {'ok': False, 'message': str(e)}


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
        display_date = _dt.strptime(date_str, '%Y-%m-%d').strftime('%d %B %Y')

        cats = [
            ('unexplained',       '&#x2717; Unexplained Breaks',     '#B03030', '#FDF1F1'),
            ('minor_break',       '&#x26A0; Minor Breaks (&lt;1)',    '#B06820', '#FFF8E1'),
            ('custody_only',      '+ Custody Only',                   '#2A4FA8', '#EEF4FF'),
            ('ws_only',           '&#x2212; WS Only',                 '#E65100', '#FFF3E0'),
            ('investor_alloc_not_communicated',
                                  '! Investor Allocation Not Communicated',
                                                                       '#8E3FA0', '#F4ECF8'),
            ('pending_explained', '&#x7E; Pending Explained',         '#B06820', '#FDF5EB'),
            ('clean',             '&#x2713; Clean Matches',           '#1A7A4A', '#EAF7F0'),
        ]

        total     = sum(len(v) for v in results.values())
        has_breaks = bool(results.get('unexplained') or
                          results.get('custody_only') or
                          results.get('ws_only') or
                          results.get('investor_alloc_not_communicated'))
        status_c  = '#B03030' if has_breaks else '#1A7A4A'
        status_t  = 'ACTION REQUIRED' if has_breaks else 'ALL CLEAR'

        # Summary table rows
        trows = ''
        for cat, label, fg, bg in cats:
            count = len(results.get(cat, []))
            bold  = 'font-weight:bold;' if count > 0 and cat in (
                'unexplained', 'custody_only', 'ws_only',
                'investor_alloc_not_communicated') else ''
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
            f'Generated by Keystone &#8212; {_branding_get()["firm_name"]}<br>'
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
        to_list, bcc_list = self._split_self_to_bcc(recipients)
        self._log_send_intent('recon', subject, to_list, bcc_list)

        msg = {
            'subject': subject,
            'body': {'contentType': 'HTML', 'content': html_body},
            'toRecipients': [{'emailAddress': {'address': r}} for r in to_list],
            'attachments': attachments,
        }
        if bcc_list:
            msg['bccRecipients'] = [{'emailAddress': {'address': r}} for r in bcc_list]
        payload = {'message': msg, 'saveToSentItems': True}

        url = f"{GRAPH_BASE}/users/{self.mailbox}/sendMail"
        try:
            resp = requests.post(url, headers=self._headers(),
                                 json=payload, timeout=30)
            resp.raise_for_status()
            sent_to = to_list + [f'{a} (bcc)' for a in bcc_list]
            logger.info(f"send[recon] graph_status={resp.status_code} OK to={sent_to}")
            return {'ok': True,
                    'message': f"Email sent to {', '.join(sent_to)}"}
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
        display_date = _dt.strptime(date_str, '%Y-%m-%d').strftime('%d %B %Y')

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
            f'Generated by Keystone &#8212; {_branding_get()["firm_name"]}<br>'
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
        to_list, bcc_list = self._split_self_to_bcc(recipients)
        self._log_send_intent('bank_recon', subject, to_list, bcc_list)

        msg_obj = {
            'subject': subject,
            'body': {'contentType': 'HTML', 'content': html_body},
            'toRecipients': [{'emailAddress': {'address': r}} for r in to_list],
            'attachments': attachments,
        }
        if bcc_list:
            msg_obj['bccRecipients'] = [{'emailAddress': {'address': r}} for r in bcc_list]
        payload = {'message': msg_obj, 'saveToSentItems': True}

        url = f"{GRAPH_BASE}/users/{self.mailbox}/sendMail"
        try:
            resp = requests.post(url, headers=self._headers(),
                                 json=payload, timeout=30)
            resp.raise_for_status()
            sent_to = to_list + [f'{a} (bcc)' for a in bcc_list]
            logger.info(f"send[bank_recon] graph_status={resp.status_code} OK to={sent_to}")
            return {'ok': True,
                    'message': f"Email sent to {', '.join(sent_to)}"}
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
            display_date = _dt.strptime(date_str, '%Y-%m-%d').strftime('%d %B %Y')
        except ValueError:
            display_date = date_str

        try:
            sent_dt    = _dt.fromisoformat(initial_sent_at)
            hours_ago  = max(1, int((_dt.utcnow() - sent_dt).total_seconds() // 3600))
            sent_label = sent_dt.strftime('%d %b %Y %H:%M UTC')
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
            f'Generated by Keystone &#8212; {_branding_get()["firm_name"]}'
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
        to_list, bcc_list = self._split_self_to_bcc(recipients)
        self._log_send_intent('reminder', subject, to_list, bcc_list)

        msg_obj = {
            'subject': subject,
            'body': {'contentType': 'HTML', 'content': html_body},
            'toRecipients': [{'emailAddress': {'address': r}} for r in to_list],
            'attachments': attachments,
        }
        if bcc_list:
            msg_obj['bccRecipients'] = [{'emailAddress': {'address': r}} for r in bcc_list]
        payload = {'message': msg_obj, 'saveToSentItems': True}

        url = f'{GRAPH_BASE}/users/{self.mailbox}/sendMail'
        try:
            resp = requests.post(url, headers=self._headers(),
                                 json=payload, timeout=30)
            resp.raise_for_status()
            sent_to = to_list + [f'{a} (bcc)' for a in bcc_list]
            logger.info(f'send[reminder] graph_status={resp.status_code} '
                        f'#{reminder_count + 1} {recon_type}/{date_str} OK to={sent_to}')
            return {'ok': True, 'message': f'Reminder sent to {", ".join(sent_to)}'}
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
        display_date = _dt.strptime(date_str, '%Y-%m-%d').strftime('%d %B %Y')
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
            f'<span style="color:#C9A84C;font-size:18px;font-weight:700">{_branding_get()["firm_short_name"]}</span>'
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
        to_list, bcc_list = self._split_self_to_bcc(recipients)
        self._log_send_intent('trade_recon', subject, to_list, bcc_list)

        msg_obj = {
            'subject': subject,
            'body': {'contentType': 'HTML', 'content': body_html},
            'toRecipients': [{'emailAddress': {'address': r}} for r in to_list],
        }
        if bcc_list:
            msg_obj['bccRecipients'] = [{'emailAddress': {'address': r}} for r in bcc_list]
        payload = {'message': msg_obj}

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
            sent_to = to_list + [f'{a} (bcc)' for a in bcc_list]
            logger.info(f'send[trade_recon] graph_status={resp.status_code} OK to={sent_to}')
            return {'ok': True, 'message': f'Trade recon email sent to {len(sent_to)} recipient(s)'}
        except requests.HTTPError as e:
            msg = f'Graph API error {e.response.status_code}: {e.response.text[:200]}'
            logger.error(f'Trade recon email failed: {msg}')
            return {'ok': False, 'message': msg}
        except Exception as e:
            logger.error(f'Trade recon email failed: {e}')
            return {'ok': False, 'message': str(e)}

    def send_workflow_summary(self, *, kind: str, date_str: str,
                              recipients: list,
                              errors: list[str] | None = None,
                              detail_lines: list[str] | None = None,
                              pdf_summary: str = '',
                              pdf_sections: dict | None = None,
                              attachment_path: str = '',
                              attachment_content_type: str = 'application/pdf'
                              ) -> dict:
        """Send a BoD or EoD pipeline completion email.

        Generic equivalent of ``send_recon_summary`` for the workflow
        pipelines (Morning BoD, Evening EoD). Recipients come from the
        same ``recon_recipients`` list in azure config — caller is
        responsible for fetching them.

        Body sections (rendered top-to-bottom, only non-empty appear):
          - Status banner: green "ALL CLEAR" or red "ACTION REQUIRED".
          - Errors block (red) — one bullet per error string.
          - Detail lines (neutral) — short status notes from the pipeline.
          - PDF section summary (EoD only) — section name + truncated rows.

        ``attachment_path`` is optional; for EoD this is the recon PDF.
        """
        if not self.is_configured():
            return {'ok': False, 'message': 'Azure credentials not configured.'}
        if not recipients:
            return {'ok': False, 'message': 'No recipients specified.'}

        from datetime import datetime as _dt
        try:
            display_date = _dt.strptime(date_str, '%Y-%m-%d').strftime('%d %B %Y')
        except ValueError:
            display_date = date_str
        errors = errors or []
        detail_lines = detail_lines or []
        pdf_sections = pdf_sections or {}

        has_errors = bool(errors)
        status_c = '#B03030' if has_errors else '#1A7A4A'
        status_t = 'ACTION REQUIRED' if has_errors else 'ALL CLEAR'
        subject_prefix = '[ACTION]' if has_errors else '[OK]'
        subject = f'{subject_prefix} {kind} {display_date} — {status_t}'

        def _esc(s: str) -> str:
            return (s.replace('&', '&amp;').replace('<', '&lt;')
                     .replace('>', '&gt;'))

        parts: list[str] = [
            f'<div style="font-family:Arial,sans-serif;color:#222">',
            f'<div style="background:{status_c};color:#fff;padding:14px 18px;'
            f'border-radius:6px;margin-bottom:16px">'
            f'<div style="font-size:12px;letter-spacing:1px;opacity:.85">'
            f'{kind.upper()} STATUS</div>'
            f'<div style="font-size:20px;font-weight:bold;margin-top:4px">'
            f'{status_t}</div>'
            f'<div style="font-size:13px;margin-top:6px">{display_date}</div>'
            f'</div>',
        ]

        if has_errors:
            parts.append(
                f'<h3 style="color:#B03030;margin:18px 0 8px">'
                f'Errors ({len(errors)})</h3>'
                f'<ul style="font-size:13px;line-height:1.55">'
                + ''.join(f'<li>{_esc(e)}</li>' for e in errors)
                + '</ul>'
            )

        if detail_lines:
            parts.append(
                f'<h3 style="color:#444;margin:18px 0 8px">Pipeline notes</h3>'
                f'<ul style="font-size:13px;line-height:1.55;color:#444">'
                + ''.join(f'<li>{_esc(d)}</li>' for d in detail_lines)
                + '</ul>'
            )

        if pdf_summary:
            parts.append(
                f'<h3 style="color:#444;margin:22px 0 8px">'
                f'Reconciliation Statement Summary</h3>'
                f'<p style="font-size:13px;color:#444;margin:0 0 10px">'
                f'{_esc(pdf_summary)}</p>'
            )
        if pdf_sections:
            parts.append(
                '<table style="border-collapse:collapse;width:100%;'
                'font-family:Arial,sans-serif;font-size:12px">'
            )
            for name, rows in pdf_sections.items():
                parts.append(
                    f'<tr><td colspan="2" style="background:#FAFCFF;'
                    f'padding:8px 12px;border-bottom:1px solid #DDE3EF;'
                    f'font-weight:bold;color:#333">'
                    f'{_esc(name)} ({len(rows)} row'
                    f'{"" if len(rows)==1 else "s"})</td></tr>'
                )
                head = rows[:25]
                for r in head:
                    parts.append(
                        f'<tr><td style="padding:4px 12px;border-bottom:'
                        f'1px solid #eee;color:#555;font-size:12px">'
                        f'• {_esc(r)}</td></tr>'
                    )
                extra = len(rows) - len(head)
                if extra > 0:
                    parts.append(
                        f'<tr><td style="padding:4px 12px;color:#888;'
                        f'font-style:italic">'
                        f'… (+{extra} more — see attached PDF)'
                        f'</td></tr>'
                    )
            parts.append('</table>')

        parts.append(
            f'<p style="font-size:11px;color:#888;margin:24px 0 0;'
            f'border-top:1px solid #e0e5ed;padding-top:12px">'
            f'Generated by Keystone &#8212; {_branding_get()["firm_name"]}</p>'
            f'</div>'
        )
        html_body = '\n'.join(parts)

        recipients, html_body = self._apply_test_mode(recipients, html_body)
        to_list, bcc_list = self._split_self_to_bcc(recipients)
        self._log_send_intent(f'workflow.{kind}', subject, to_list, bcc_list)

        attachments = []
        if attachment_path and os.path.exists(attachment_path):
            try:
                import base64 as _b64
                with open(attachment_path, 'rb') as _f:
                    file_bytes = _f.read()
                attachments = [{
                    '@odata.type':  '#microsoft.graph.fileAttachment',
                    'name':         os.path.basename(attachment_path),
                    'contentType':  attachment_content_type,
                    'contentBytes': _b64.b64encode(file_bytes).decode('utf-8'),
                }]
                logger.info(f"Attaching file: "
                            f"{os.path.basename(attachment_path)} "
                            f"({len(file_bytes):,} bytes)")
            except Exception as e:
                logger.warning(f"Could not attach file: {e}")

        msg_obj = {
            'subject': subject,
            'body': {'contentType': 'HTML', 'content': html_body},
            'toRecipients': [{'emailAddress': {'address': r}} for r in to_list],
            'attachments': attachments,
        }
        if bcc_list:
            msg_obj['bccRecipients'] = [{'emailAddress': {'address': r}} for r in bcc_list]
        payload = {'message': msg_obj, 'saveToSentItems': True}

        url = f"{GRAPH_BASE}/users/{self.mailbox}/sendMail"
        try:
            resp = requests.post(url, headers=self._headers(),
                                 json=payload, timeout=30)
            resp.raise_for_status()
            sent_to = to_list + [f'{a} (bcc)' for a in bcc_list]
            logger.info(f"send[workflow.{kind}] graph_status={resp.status_code} OK to={sent_to}")
            return {'ok': True,
                    'message': f"{kind} email sent to {', '.join(sent_to)}"}
        except requests.HTTPError as e:
            msg = (f"Graph API error {e.response.status_code}: "
                   f"{e.response.text[:300]}")
            logger.error(f"{kind} workflow email failed: {msg}")
            return {'ok': False, 'message': msg}
        except Exception as e:
            logger.error(f"{kind} workflow email failed: {e}")
            return {'ok': False, 'message': str(e)}


    def archive_for_date(self, date_str: str, sources: List[dict],
                         archive_base_dir: str,
                         log_callback=None,
                         since: Optional[str] = None,
                         mailbox_override: str = '') -> dict:
        """
        Archive all emails (body + attachments) from configured sender addresses
        for the lookback window around date_str (or narrowed via `since`).

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
        # around date_str — the full admin-override window is separately
        # driven by each day's own archive_for_date call.
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
