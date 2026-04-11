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

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import __version__
from .config import AgentSettings, load_settings, save_settings
from .pairing import get_coordinator, unpair
from .secrets import smoke_test

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
    <a href="/pair">Pair</a>
    <a href="/api/status">JSON status</a>
    <a href="/api/dpapi-test">DPAPI self-test</a>
  </div>
"""


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

    return app


def get_or_initialise_settings() -> AgentSettings:
    """Ensure we have a valid settings file before boot."""
    settings = load_settings()
    save_settings(settings)
    return settings
