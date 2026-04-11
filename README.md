# Keystone — Agent

On-premise reconciliation agent. Runs as a Windows service, listens on
`localhost:5001` for a local admin UI, and outbound-polls the Keystone
control plane for work.

**Phase 0 scaffold.** Boots, serves the local UI, runs a poll loop, and
has a working DPAPI-backed secret store. No real reconciliation yet —
Phase 1 ports the engine modules from `wealthspectrum-helper`.

## Prereqs

- Python 3.12 (NOT 3.14 — the install ships with `py -3.12` on Windows)
- Windows 10/11 or Server 2019+ for the DPAPI and service paths
- A running control plane (see `../keystone-control-plane`)

## Setup (dev)

```powershell
cd C:\Users\Administrator\keystone-agent
py -3.12 -m venv .venv
.\.venv\Scripts\activate
pip install -e .[dev]
```

## Run (foreground)

```powershell
py -3.12 -m agent.main
```

- Local UI: http://127.0.0.1:5001/
- JSON status: http://127.0.0.1:5001/api/status
- DPAPI self-test: http://127.0.0.1:5001/api/dpapi-test

Environment overrides:

- `KEYSTONE_DATA_DIR` — where to write `agent-config.json`, secrets, logs.
  Default on Windows: `%ProgramData%\Keystone`.

## Install as a Windows service

Build the PyInstaller bundle first, then:

```powershell
.\installer\dist\KeystoneAgent\KeystoneAgent.exe install
.\installer\dist\KeystoneAgent\KeystoneAgent.exe start
```

See [installer/README.md](installer/README.md) for the full build pipeline.

## Layout

```
agent/
  protocol.py    pydantic models for the wire format (mirrors lib/protocol.ts)
  config.py      agent-config.json load/save + data-dir resolution
  secrets.py     DPAPI-backed secret store (+ fallback for dev on non-Windows)
  transport.py   httpx client for the 4 control-plane endpoints
  poll.py        background poll loop (heartbeat + job fetch)
  local_ui.py    FastAPI app on localhost:5001
  service.py     pywin32 Windows service wrapper
  main.py        foreground entry point used by `py -m agent.main`

installer/
  keystone-agent.spec    PyInstaller spec
  keystone-agent.iss     Inno Setup script
  README.md              build instructions

core/      (Phase 1) ported from wealthspectrum-helper
parsers/   (Phase 1) ported from wealthspectrum-helper
```

## What the agent will NOT do, ever

- Send raw holdings, ISINs, client names, or custodian statements to the
  control plane. Only summary counts and statuses cross the wire. This
  is enforced at the push boundary in `agent/runner.py` (Phase 1).
- Listen on anything other than 127.0.0.1. The local UI is explicitly
  not reachable from the rest of the customer's network.
- Store credentials in plaintext. Everything sensitive goes through the
  DPAPI-backed secret store on Windows.
