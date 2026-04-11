# Keystone agent — installer

Build pipeline for the Windows installer.

## Phase 0 scope

Two scripts, unsigned output:

- `keystone-agent.spec` — PyInstaller spec. Produces a one-folder bundle
  under `installer/dist/KeystoneAgent/`.
- `keystone-agent.iss` — Inno Setup script. Wraps the PyInstaller folder
  in an `.exe` installer under `installer/output/`.

## Build (dev)

From the agent repo root with Python 3.12:

```powershell
# One-time: install build deps in your virtualenv
py -3.12 -m pip install -e .[build]

# PyInstaller
py -3.12 -m PyInstaller installer/keystone-agent.spec --distpath installer/dist --workpath installer/build

# Inno Setup (requires Inno Setup 6+ on PATH or set ISCC manually)
iscc.exe installer/keystone-agent.iss
```

## What's missing until Phase 4

- **Code signing.** Phase 4 wires SignTool into both the PyInstaller
  output and the final installer. Until then Windows SmartScreen will
  warn users.
- **Auto-updater.** The installer doesn't yet register an update channel.
  Phase 4 adds a Squirrel/WiX-Burn style self-update flow that pulls
  from the `agent-installers` blob container.
- **Silent install flags.** The Inno script already supports `/silent`
  and `/verysilent` but isn't wired to customer-provided pairing tokens.
- **Uninstall safety.** The uninstaller stops and removes the service
  but doesn't (yet) wipe DPAPI secrets — Phase 1 decision on whether
  that's the right default.
