"""Local FastAPI UI running on 127.0.0.1:5001.

Phase 1 additions:
  - Pair page (GET /pair) with a wizard form to start pairing
  - POST /api/pair/start to kick off the background poll
  - GET  /api/pair/status so the wizard can poll for status
  - POST /api/pair/cancel to abort in-progress pairing
  - POST /api/unpair to forget the current pairing
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import __version__
from .config import AgentSettings, load_settings, save_settings
from .local_runs import get_store as _get_local_runs
from .pairing import get_coordinator, unpair
from .secrets import smoke_test
from .setup import (
    CUSTODIANS_WITH_ZIP_PASSWORDS,
    save_custodian_password,
    save_m365,
    save_workdir,
    save_ws_portal,
    setup_state,
)
from .template_drafts import get_store as _get_draft_store
from .templates import (
    commit_csv,
    export_csv,
    get_template,
    list_templates,
    parse_and_diff,
)

logger = logging.getLogger(__name__)

# Local UI binds only to 127.0.0.1 — never the network interface.
LOCAL_UI_HOST = "127.0.0.1"
LOCAL_UI_PORT = 5001


# ── Shared page chrome ──────────────────────────────────────────────── #

_PAGE_STYLE = """
<style>
  body { font-family: -apple-system, Segoe UI, Lato, sans-serif;
         max-width: 820px; margin: 40px auto; padding: 0 24px;
         color: #1a2535; background: #f4f6f9; }
  h1 { color: #2a4fa8; font-size: 22px; margin-bottom: 4px; }
  .brand { color: #c9a84c; font-size: 14px; letter-spacing: 0.1em;
           text-transform: uppercase; font-weight: 700; }
  .nav { display: flex; gap: 16px; margin: 14px 0 0; font-size: 12px; }
  .nav a { color: #2a4fa8; text-decoration: none; font-weight: 700; }
  .nav a:hover { text-decoration: underline; }
  .card { background: #fff; border: 1px solid #dde2eb; border-radius: 6px;
          padding: 20px 24px; margin-top: 16px;
          box-shadow: 0 2px 8px rgba(11,25,41,.06); }
  .card-title { font-size: 11px; font-weight: 700; text-transform: uppercase;
                letter-spacing: .06em; color: #6b7c93; margin-bottom: 10px; }
  .badge { display: inline-block; padding: 3px 10px; border-radius: 4px;
           color: white; font-size: 10px; font-weight: 700; letter-spacing: .06em; }
  .paired   { background: #1a7a4a; }
  .unpaired { background: #b06820; }
  dl { margin: 0; display: grid; grid-template-columns: 160px 1fr; gap: 8px 16px; font-size: 12px; }
  dt { color: #6b7c93; font-weight: 700; text-transform: uppercase; font-size: 10px; }
  dd { margin: 0; font-family: 'DM Mono', Consolas, monospace; }
  .code-big { font-family: 'DM Mono', Consolas, monospace; font-size: 34px;
              font-weight: 700; letter-spacing: .08em; color: #2a4fa8;
              text-align: center; padding: 24px; background: #eef4ff;
              border: 2px solid #bdd0f8; border-radius: 8px; }
  input[type=text], input[type=url] { width: 100%; padding: 8px 12px;
              border: 1px solid #dde2eb; border-radius: 4px; font-size: 13px;
              font-family: inherit; }
  label { font-size: 10px; font-weight: 700; text-transform: uppercase;
          letter-spacing: .06em; color: #6b7c93; display: block; margin-bottom: 4px; }
  button { cursor: pointer; padding: 8px 18px; border-radius: 4px;
           font-weight: 700; font-size: 12px; border: none; background: #2a4fa8; color: white; }
  button.secondary { background: #fff; color: #2a4fa8; border: 1px solid #2a4fa8; }
  .muted { color: #6b7c93; font-size: 11px; }
  .err { color: #b03030; font-size: 11px; font-weight: 700; }
  .ok  { color: #1a7a4a; font-size: 11px; font-weight: 700; }
</style>
"""


def _page_header(settings: AgentSettings) -> str:
    paired = bool(settings.agent_id and settings.has_token)
    badge = (
        '<span class="badge paired">PAIRED</span>'
        if paired
        else '<span class="badge unpaired">UNPAIRED</span>'
    )
    return f"""
  <div class="brand">Keystone</div>
  <h1>Reconciliation Agent {badge}</h1>
  <p class="muted">Version {__version__} · listening on 127.0.0.1 only</p>
  <div class="nav">
    <a href="/">Status</a>
    <a href="/setup">Setup</a>
    <a href="/templates">Templates</a>
    <a href="/pair">Pair</a>
    <a href="/api/status">JSON status</a>
    <a href="/api/dpapi-test">DPAPI self-test</a>
  </div>
"""


def _setup_banner() -> str:
    """Return a red strip urging the operator to /setup if anything is
    missing. Used at the top of the status and pair pages."""
    state = setup_state()
    if state["all_done"]:
        return ""
    return (
        '<div class="card" style="border:1px solid #f5b8b8;background:#fdf1f1">'
        '<div class="card-title" style="color:#b03030">Setup incomplete</div>'
        '<p style="color:#b03030;font-size:13px;margin:6px 0">'
        f'The agent is missing configuration for step <b>{state["next_incomplete"]}</b>. '
        'Recon jobs will fail until setup is complete.</p>'
        '<p style="margin-top:10px"><a href="/setup" '
        'style="display:inline-block;padding:6px 14px;background:#2a4fa8;color:white;'
        'text-decoration:none;border-radius:4px;font-weight:700;font-size:12px">'
        'Finish setup →</a></p>'
        '</div>'
    )


# ── FastAPI app ─────────────────────────────────────────────────────── #


def create_app() -> FastAPI:
    app = FastAPI(title="Keystone Agent", version=__version__)

    @app.get("/", response_class=HTMLResponse)
    def root() -> HTMLResponse:
        settings = load_settings()
        html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Keystone Agent</title>
{_PAGE_STYLE}</head><body>
{_page_header(settings)}
{_setup_banner()}

<div class="card">
  <div class="card-title">Agent</div>
  <dl>
    <dt>Name</dt><dd>{settings.agent_name or '<span class="err">not set — pair to choose one</span>'}</dd>
    <dt>Fingerprint</dt><dd>{settings.machine_fingerprint}</dd>
    <dt>Workdir</dt><dd>{settings.workdir or '—'}</dd>
    <dt>Control plane</dt><dd>{settings.control_plane_url}</dd>
    <dt>Poll interval</dt><dd>{settings.poll_interval_seconds}s</dd>
  </dl>
</div>

<div class="card">
  <div class="card-title">Pairing</div>
  <dl>
    <dt>Agent ID</dt><dd>{settings.agent_id or '—'}</dd>
    <dt>Tenant ID</dt><dd>{settings.tenant_id or '—'}</dd>
    <dt>Token</dt><dd>{'stored in DPAPI' if settings.has_token else '—'}</dd>
  </dl>
</div>

<div class="card">
  <div class="card-title">Diagnostics</div>
  <dl>
    <dt>Server time</dt><dd>{datetime.now(timezone.utc).isoformat()}</dd>
  </dl>
</div>
</body></html>"""
        return HTMLResponse(html)

    @app.get("/pair", response_class=HTMLResponse)
    def pair_page() -> HTMLResponse:
        settings = load_settings()
        coord = get_coordinator()
        session = coord.status()

        # If already paired, show an unpair option instead of the wizard.
        if settings.agent_id and settings.has_token:
            body = f"""
<div class="card">
  <div class="card-title">Already paired</div>
  <p class="muted">This agent is paired with tenant <code>{settings.tenant_id}</code> as <code>{settings.agent_id}</code>.</p>
  <form action="/api/unpair" method="post" style="margin-top:16px">
    <button type="submit" class="secondary">Unpair this agent</button>
  </form>
  <p class="muted" style="margin-top:10px">Unpairing forgets the token on this machine. The agents row on the control plane is not touched — Phase 2 feature.</p>
</div>"""
        elif session is not None and session.state in ("showing_code", "error"):
            body = f"""
<div class="card">
  <div class="card-title">Enter this code on the control plane</div>
  <div class="code-big">{session.code}</div>
  <p class="muted" style="text-align:center;margin-top:12px">
    Open the control plane in a browser, sign in, go to Agents → Pair new agent, and type this code.
    The agent will finish pairing automatically within a minute.
  </p>
  <p id="ppoll" class="muted" style="text-align:center">Waiting for claim…</p>
  <form action="/api/pair/cancel" method="post" style="text-align:center;margin-top:16px">
    <button type="submit" class="secondary">Cancel</button>
  </form>
</div>
<script>
  async function poll() {{
    try {{
      const r = await fetch('/api/pair/status');
      const j = await r.json();
      const p = document.getElementById('ppoll');
      if (j.state === 'claimed') {{
        p.className = 'ok'; p.textContent = 'Paired! Redirecting…';
        setTimeout(() => location.href = '/', 1200);
        return;
      }}
      if (j.state === 'expired') {{
        p.className = 'err'; p.textContent = 'Code expired. Refresh to generate a new one.';
        return;
      }}
      if (j.state === 'error') {{
        p.className = 'err'; p.textContent = 'Error: ' + (j.message || 'unknown');
      }}
      setTimeout(poll, 2000);
    }} catch (e) {{ setTimeout(poll, 4000); }}
  }}
  poll();
</script>
"""
        elif session is not None and session.state == "claimed":
            body = f"""
<div class="card">
  <div class="card-title ok">Paired</div>
  <p>Agent {settings.agent_id} is now paired with tenant {settings.tenant_id}.</p>
  <p><a href="/">Back to status</a></p>
</div>"""
        else:
            # Idle / expired / no session — show start form
            prior_name = settings.agent_name
            err_html = (
                f'<p class="err">{session.message}</p>'
                if session is not None and session.message
                else ""
            )
            body = f"""
<div class="card">
  <div class="card-title">Pair this agent</div>
  <p class="muted">Give this agent a friendly name, then click Start. You will see a device code to enter on the control plane.</p>
  {err_html}
  <form action="/api/pair/start" method="post" style="margin-top:16px">
    <label for="agent_name">Agent name</label>
    <input id="agent_name" type="text" name="agent_name" required minlength="1" maxlength="120"
           placeholder="e.g. Mumbai ops server"
           value="{prior_name}">
    <p class="muted" style="margin:6px 0 16px">This is how the control plane will label this agent in the admin UI.</p>

    <label for="control_plane_url">Control plane URL</label>
    <input id="control_plane_url" type="url" name="control_plane_url" required
           value="{settings.control_plane_url}">
    <p class="muted" style="margin:6px 0 16px">Where to reach the Keystone control plane. Leave as default during dev.</p>

    <button type="submit">Start pairing</button>
  </form>
</div>"""

        html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Pair · Keystone Agent</title>
{_PAGE_STYLE}</head><body>
{_page_header(settings)}
{body}
</body></html>"""
        return HTMLResponse(html)

    # ── JSON endpoints ──────────────────────────────────────────────── #

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        settings = load_settings()
        return JSONResponse(
            {
                "version": __version__,
                "agent_name": settings.agent_name,
                "agent_id": settings.agent_id or None,
                "tenant_id": settings.tenant_id or None,
                "paired": bool(settings.agent_id and settings.has_token),
                "control_plane_url": settings.control_plane_url,
                "poll_interval_seconds": settings.poll_interval_seconds,
                "machine_fingerprint": settings.machine_fingerprint,
                "workdir": settings.workdir,
                "server_time": datetime.now(timezone.utc).isoformat(),
            }
        )

    @app.get("/api/dpapi-test")
    def api_dpapi_test() -> JSONResponse:
        try:
            result = smoke_test()
            return JSONResponse({"ok": True, **result})
        except Exception as e:  # noqa: BLE001
            logger.exception("DPAPI smoke test failed")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.post("/api/pair/start")
    async def api_pair_start(request: Request) -> Any:
        form = await request.form()
        agent_name = str(form.get("agent_name", "")).strip()
        control_plane_url = str(form.get("control_plane_url", "")).strip()
        if not agent_name:
            return RedirectResponse(url="/pair?err=name_required", status_code=303)
        if control_plane_url:
            settings = load_settings()
            settings.control_plane_url = control_plane_url
            save_settings(settings)
        get_coordinator().start(agent_name=agent_name)
        return RedirectResponse(url="/pair", status_code=303)

    @app.get("/api/pair/status")
    def api_pair_status() -> JSONResponse:
        session = get_coordinator().status()
        if session is None:
            return JSONResponse({"state": "idle"})
        return JSONResponse(
            {
                "state": session.state,
                "code": session.code,
                "message": session.message,
                "agent_id": session.agent_id,
                "tenant_id": session.tenant_id,
            }
        )

    @app.post("/api/pair/cancel")
    def api_pair_cancel() -> Any:
        get_coordinator().cancel()
        return RedirectResponse(url="/pair", status_code=303)

    @app.post("/api/unpair")
    def api_unpair() -> Any:
        get_coordinator().cancel()
        unpair()
        return RedirectResponse(url="/pair", status_code=303)

    # ── First-run wizard ───────────────────────────────────────────── #

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(msg: str | None = None, err: str | None = None) -> HTMLResponse:
        settings = load_settings()
        state = setup_state()
        extras = state["extras"]
        secret_flags = state["secret_flags"]

        def _step_badge(done: bool) -> str:
            return (
                '<span class="badge paired">DONE</span>'
                if done
                else '<span class="badge unpaired">TODO</span>'
            )

        def _secret_state(flag: bool) -> str:
            if flag:
                return '<span class="muted">(stored — leave blank to keep)</span>'
            return '<span class="err">(not yet set)</span>'

        msg_html = (
            f'<div class="card" style="border:1px solid #a3d9bc;background:#eaf7f0"><p class="ok">{msg}</p></div>'
            if msg
            else ""
        )
        err_html = (
            f'<div class="card" style="border:1px solid #f5b8b8;background:#fdf1f1"><p class="err">{err}</p></div>'
            if err
            else ""
        )

        custodian_rows = ""
        for src in CUSTODIANS_WITH_ZIP_PASSWORDS:
            flag_key = f"custodian_zip_password_{src}"
            already = secret_flags.get(flag_key, False)
            custodian_rows += (
                f'<div style="margin-top:12px">'
                f'<label>{src.upper()} zip password</label>'
                f'<input type="password" name="{src}" placeholder="(enter once to save)" autocomplete="off">'
                f'<div style="margin-top:4px">{_secret_state(already)}</div>'
                f'</div>'
            )

        body = f"""
<div class="card">
  <div class="card-title">First-run wizard</div>
  <p class="muted">Fill in each step below. All secrets are encrypted at rest via Windows DPAPI.</p>
  <ol style="font-size:12px;line-height:1.7;margin:10px 0">
    {''.join(f'<li>{_step_badge(s["completed"])} <b>{s["title"]}</b> — {s["description"]}</li>' for s in state["steps"])}
  </ol>
</div>

{msg_html}
{err_html}

<div class="card">
  <div class="card-title">Working directory</div>
  <form action="/api/setup/workdir" method="post">
    <label for="workdir">Workdir path</label>
    <input type="text" id="workdir" name="workdir" value="{settings.workdir or ''}" required>
    <p class="muted" style="margin:6px 0 12px">
      The agent reads raw custodian files from <code>{{workdir}}/data/{{date}}/raw/{{source}}/</code>
      and writes recon reports to <code>{{workdir}}/data/{{date}}/output/</code>.
    </p>
    <button type="submit">Save workdir</button>
  </form>
</div>

<div class="card">
  <div class="card-title">Microsoft 365 / Graph credentials</div>
  <form action="/api/setup/m365" method="post" autocomplete="off">
    <label for="m365_tenant_id">Tenant ID</label>
    <input type="text" id="m365_tenant_id" name="m365_tenant_id" value="{extras.get('m365_tenant_id','')}" required>
    <label for="m365_client_id" style="margin-top:12px">Client (application) ID</label>
    <input type="text" id="m365_client_id" name="m365_client_id" value="{extras.get('m365_client_id','')}" required>
    <label for="m365_mailbox" style="margin-top:12px">Shared mailbox address</label>
    <input type="text" id="m365_mailbox" name="m365_mailbox" value="{extras.get('m365_mailbox','')}" required>
    <label for="m365_client_secret" style="margin-top:12px">Client secret</label>
    <input type="password" id="m365_client_secret" name="m365_client_secret" placeholder="(enter once to save)" autocomplete="off">
    <div style="margin-top:4px">{_secret_state(secret_flags.get('m365_client_secret', False))}</div>
    <button type="submit" style="margin-top:16px">Save M365 credentials</button>
  </form>
</div>

<div class="card">
  <div class="card-title">WS portal credentials</div>
  <form action="/api/setup/ws-portal" method="post" autocomplete="off">
    <label for="ws_username">WS portal username</label>
    <input type="text" id="ws_username" name="ws_username" value="{extras.get('ws_portal_username','')}" required>
    <label for="ws_client_code" style="margin-top:12px">Client code</label>
    <input type="text" id="ws_client_code" name="ws_client_code" value="{extras.get('ws_portal_client_code','')}" required>
    <label for="ws_password" style="margin-top:12px">Portal password</label>
    <input type="password" id="ws_password" name="ws_password" placeholder="(enter once to save)" autocomplete="off">
    <div style="margin-top:4px">{_secret_state(secret_flags.get('ws_portal_password', False))}</div>
    <button type="submit" style="margin-top:16px">Save WS credentials</button>
  </form>
</div>

<div class="card">
  <div class="card-title">Custodian zip passwords</div>
  <p class="muted">HDFC and Axis ship statements inside encrypted zip files. Enter each password once.</p>
  <form action="/api/setup/custodian-passwords" method="post" autocomplete="off">
    {custodian_rows}
    <button type="submit" style="margin-top:16px">Save custodian passwords</button>
  </form>
</div>
"""
        html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Setup · Keystone Agent</title>
{_PAGE_STYLE}
<style>
  label {{ display:block; font-size:11px; font-weight:700; text-transform:uppercase;
            letter-spacing:.05em; color:#6b7c93; margin-bottom:4px }}
  input[type=text], input[type=password] {{ width:100%; padding:8px 12px;
            border:1px solid #dde2eb; border-radius:4px; font-size:13px;
            font-family:var(--mono,Consolas),monospace }}
  .card-title {{ font-size:13px; font-weight:700; color:#2a4fa8; margin-bottom:8px }}
  .ok {{ color:#1a7a4a; font-size:13px }}
  .err {{ color:#b03030; font-size:13px }}
</style>
</head><body>
{_page_header(load_settings())}
{body}
</body></html>"""
        return HTMLResponse(html)

    @app.post("/api/setup/workdir")
    async def api_setup_workdir(request: Request) -> Any:
        form = await request.form()
        workdir = str(form.get("workdir", "")).strip()
        if not workdir:
            return RedirectResponse(url="/setup?err=Workdir+required", status_code=303)
        save_workdir(workdir)
        return RedirectResponse(url="/setup?msg=Workdir+saved", status_code=303)

    @app.post("/api/setup/m365")
    async def api_setup_m365(request: Request) -> Any:
        form = await request.form()
        try:
            save_m365(
                tenant_id=str(form.get("m365_tenant_id", "")),
                client_id=str(form.get("m365_client_id", "")),
                client_secret=str(form.get("m365_client_secret", "")) or None,
                mailbox=str(form.get("m365_mailbox", "")),
            )
        except Exception as e:  # noqa: BLE001
            return RedirectResponse(url=f"/setup?err={e}", status_code=303)
        return RedirectResponse(url="/setup?msg=M365+credentials+saved", status_code=303)

    @app.post("/api/setup/ws-portal")
    async def api_setup_ws_portal(request: Request) -> Any:
        form = await request.form()
        try:
            save_ws_portal(
                username=str(form.get("ws_username", "")),
                client_code=str(form.get("ws_client_code", "")),
                password=str(form.get("ws_password", "")) or None,
            )
        except Exception as e:  # noqa: BLE001
            return RedirectResponse(url=f"/setup?err={e}", status_code=303)
        return RedirectResponse(url="/setup?msg=WS+credentials+saved", status_code=303)

    @app.post("/api/setup/custodian-passwords")
    async def api_setup_custodians(request: Request) -> Any:
        form = await request.form()
        saved = 0
        for src in CUSTODIANS_WITH_ZIP_PASSWORDS:
            value = str(form.get(src, "")).strip()
            if value:
                try:
                    save_custodian_password(src, value)
                    saved += 1
                except Exception as e:  # noqa: BLE001
                    return RedirectResponse(
                        url=f"/setup?err={src}: {e}", status_code=303
                    )
        return RedirectResponse(
            url=f"/setup?msg={saved}+password(s)+saved", status_code=303
        )

    # ── Templates library ─────────────────────────────────────────── #

    @app.get("/templates", response_class=HTMLResponse)
    def templates_page() -> HTMLResponse:
        specs = list_templates()
        rows_html = ""
        for t in specs:
            col_help = "".join(
                f'<li><code>{c["name"]}</code>{" *" if c["required"] else ""} — {c["help"] or c["kind"]}</li>'
                for c in t["columns"]
            )
            rows_html += f"""
<div class="card">
  <div class="card-title">{t["title"]}</div>
  <p class="muted">{t["description"]}</p>
  <details style="margin-top:10px">
    <summary style="cursor:pointer;font-size:11px;color:#6b7c93">Columns ({len(t["columns"])})</summary>
    <ul style="font-size:11px;color:#6b7c93;margin:8px 0 0 16px;line-height:1.6">{col_help}</ul>
  </details>
  <div style="margin-top:14px;display:flex;gap:10px;flex-wrap:wrap">
    <a href="/api/templates/{t["slug"]}/export"
       style="padding:6px 14px;background:#2a4fa8;color:white;text-decoration:none;border-radius:4px;font-weight:700;font-size:12px">
       &darr; Download current as CSV</a>
    <form action="/api/templates/{t["slug"]}/upload" method="post" enctype="multipart/form-data"
          style="display:inline-flex;gap:8px;align-items:center">
      <input type="file" name="csv" accept=".csv,text/csv" required>
      <button type="submit">Upload &amp; preview</button>
    </form>
  </div>
</div>
"""
        html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Templates · Keystone Agent</title>
{_PAGE_STYLE}
<style>
  details summary::-webkit-details-marker {{ color:#6b7c93 }}
  input[type=file] {{ font-size:11px; font-family:var(--mono,Consolas),monospace }}
</style>
</head><body>
{_page_header(load_settings())}
<div class="card">
  <div class="card-title">Templates</div>
  <p class="muted">Export a template to CSV, edit it in Excel, upload it back.
  Keystone will validate every row and show you a diff before committing.</p>
</div>
{rows_html}
</body></html>"""
        return HTMLResponse(html)

    @app.get("/api/templates/{slug}/export")
    def api_template_export(slug: str) -> Any:
        from fastapi import HTTPException
        from fastapi.responses import Response

        try:
            csv_text = export_csv(slug)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown template")
        filename = f"keystone-{slug}.csv"
        return Response(
            content=csv_text,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.post("/api/templates/{slug}/upload", response_class=HTMLResponse)
    async def api_template_upload(slug: str, request: Request) -> HTMLResponse:
        form = await request.form()
        upload = form.get("csv")
        if upload is None or not hasattr(upload, "read"):
            return HTMLResponse(
                _template_error_page(slug, "No file uploaded"), status_code=400
            )
        # type: ignore[attr-defined]
        csv_bytes = await upload.read()  # type: ignore[union-attr]
        try:
            csv_text = csv_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            return HTMLResponse(
                _template_error_page(slug, "File is not UTF-8"), status_code=400
            )

        try:
            diff, _merged = parse_and_diff(slug, csv_text)
        except KeyError:
            return HTMLResponse(_template_error_page(slug, "Unknown template"), status_code=404)

        # Stash the CSV in the server-side scratch store and pass only
        # the opaque token through the preview URL. The commit endpoint
        # re-parses the draft so the operator sees identical numbers.
        token = _get_draft_store().put(slug, csv_text)
        return HTMLResponse(_template_preview_page(slug, diff, token))

    @app.get("/api/templates/{slug}/preview", response_class=HTMLResponse)
    def api_template_preview(slug: str, token: str) -> Any:
        draft = _get_draft_store().get(token)
        if draft is None or draft.slug != slug:
            return HTMLResponse(
                _template_error_page(slug, "Preview expired — please upload again."),
                status_code=404,
            )
        try:
            diff, _merged = parse_and_diff(slug, draft.csv_text)
        except KeyError:
            return HTMLResponse(_template_error_page(slug, "Unknown template"), status_code=404)
        return HTMLResponse(_template_preview_page(slug, diff, token))

    @app.post("/api/templates/{slug}/commit", response_class=HTMLResponse)
    async def api_template_commit(slug: str, request: Request) -> Any:
        form = await request.form()
        token = str(form.get("token", ""))
        keep_removed = str(form.get("keep_removed", "1")) == "1"
        if not token:
            return HTMLResponse(_template_error_page(slug, "Missing draft token"), status_code=400)
        store = _get_draft_store()
        draft = store.get(token)
        if draft is None or draft.slug != slug:
            return HTMLResponse(
                _template_error_page(slug, "Draft expired — please upload again."),
                status_code=404,
            )
        try:
            _diff, merged = parse_and_diff(slug, draft.csv_text)
            commit_csv(slug, merged, keep_removed=keep_removed)
        except KeyError:
            return HTMLResponse(_template_error_page(slug, "Unknown template"), status_code=404)
        except Exception as e:  # noqa: BLE001
            return HTMLResponse(_template_error_page(slug, f"Commit failed: {e}"), status_code=400)
        store.delete(token)
        return RedirectResponse(url="/templates?msg=saved", status_code=303)

    @app.get("/api/runs/{job_id}/download")
    def api_run_download(job_id: str) -> Any:
        """Stream the local recon artifact for a job id.

        Meant for use by the 'Open on agent machine' button in the
        control plane's Run Detail page. Only works from localhost —
        the agent never exposes this endpoint off-box.
        """
        row = _get_local_runs().get(job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No local artifact for this run")
        from pathlib import Path as _Path

        p = _Path(row.output_path)
        if not p.is_file():
            raise HTTPException(
                status_code=410, detail=f"Artifact moved or deleted: {row.output_path}"
            )
        return FileResponse(
            path=str(p),
            filename=p.name,
            media_type="application/octet-stream",
        )

    return app


# ── Template preview / error helpers ──────────────────────────────── #


def _template_error_page(slug: str, message: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Template error</title>{_PAGE_STYLE}</head><body>
{_page_header(load_settings())}
<div class="card" style="border:1px solid #f5b8b8;background:#fdf1f1">
  <div class="card-title" style="color:#b03030">Template error ({slug})</div>
  <p style="color:#b03030;font-size:13px">{message}</p>
  <p><a href="/templates">&larr; Back to templates</a></p>
</div>
</body></html>"""


def _template_preview_page(slug: str, diff: Any, token: str) -> str:
    spec = get_template(slug)
    title = spec.title if spec else slug

    errors_html = ""
    if getattr(diff, "errors", None):
        errors_html = (
            '<div class="card" style="border:1px solid #f5b8b8;background:#fdf1f1">'
            '<div class="card-title" style="color:#b03030">Validation errors</div>'
            '<ul style="margin:8px 0 0 20px;font-size:12px;color:#b03030">'
            + "".join(f"<li>{e}</li>" for e in diff.errors)
            + "</ul></div>"
        )

    def _row_summary(r: dict[str, Any]) -> str:
        # Show the row's key + first couple of interesting fields
        return (
            f'<code>{r.get("pool_id", r.get("key", "?"))}</code> '
            f'{r.get("display_name", "")}'
        )

    added_html = "".join(
        f'<li style="color:#1a7a4a">{_row_summary(r)}</li>' for r in diff.added
    ) or '<li class="muted">none</li>'
    removed_html = "".join(
        f'<li style="color:#b03030">{_row_summary(r)}</li>' for r in diff.removed
    ) or '<li class="muted">none</li>'
    changed_html = "".join(
        f'<li style="color:#b06820">{_row_summary(n)}</li>' for _, n in diff.changed
    ) or '<li class="muted">none</li>'

    has_errors = bool(getattr(diff, "errors", None))
    commit_btn = (
        '<button type="submit" disabled style="opacity:.4;cursor:not-allowed">'
        "Fix errors before committing</button>"
        if has_errors
        else '<button type="submit">Commit to config</button>'
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Preview · {title}</title>{_PAGE_STYLE}
<style>
  ul {{ margin:6px 0 0 20px; font-size:12px; line-height:1.6 }}
  .stats {{ display:flex; gap:24px; margin-top:14px }}
  .stat {{ padding:12px 18px; border:1px solid #dde2eb; border-radius:6px; background:#f4f6f9 }}
  .stat-n {{ font-size:22px; font-weight:700; color:#1a2535 }}
  .stat-l {{ font-size:10px; text-transform:uppercase; letter-spacing:.06em; color:#6b7c93 }}
</style>
</head><body>
{_page_header(load_settings())}

<div class="card">
  <div class="card-title">Preview — {title}</div>
  <p class="muted">Review the diff below. Nothing is written to disk yet.</p>
  <div class="stats">
    <div class="stat"><div class="stat-n">{len(diff.added)}</div><div class="stat-l">added</div></div>
    <div class="stat"><div class="stat-n">{len(diff.changed)}</div><div class="stat-l">changed</div></div>
    <div class="stat"><div class="stat-n">{len(diff.removed)}</div><div class="stat-l">removed</div></div>
    <div class="stat"><div class="stat-n">{diff.unchanged_count}</div><div class="stat-l">unchanged</div></div>
  </div>
</div>

{errors_html}

<div class="card">
  <div class="card-title">Added</div>
  <ul>{added_html}</ul>
</div>
<div class="card">
  <div class="card-title">Changed</div>
  <ul>{changed_html}</ul>
</div>
<div class="card">
  <div class="card-title">Removed from upload (still in current config)</div>
  <ul>{removed_html}</ul>
</div>

<form action="/api/templates/{slug}/commit" method="post">
  <input type="hidden" name="token" value="{token}">
  <div class="card">
    <label style="display:flex;gap:8px;align-items:center;font-size:13px">
      <input type="checkbox" name="keep_removed" value="1" checked>
      Keep rows that are missing from the upload (safer — uncheck only if you meant to delete them)
    </label>
    <div style="margin-top:16px;display:flex;gap:10px">
      {commit_btn}
      <a href="/templates" style="padding:8px 16px;border:1px solid #2a4fa8;color:#2a4fa8;text-decoration:none;border-radius:4px;font-weight:700;font-size:12px">Cancel</a>
    </div>
  </div>
</form>
</body></html>"""


def get_or_initialise_settings() -> AgentSettings:
    """Ensure we have a valid settings file before boot."""
    settings = load_settings()
    save_settings(settings)
    return settings
