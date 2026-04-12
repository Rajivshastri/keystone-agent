"""First-run wizard support — everything the operator needs to fill in
before the agent can actually run any recons.

The local UI exposes /setup as a multi-step wizard. This module owns:
  - the definition of each step
  - the completion predicate (has this step been done?)
  - the next-incomplete-step pointer
  - the save helpers that persist each step via DPAPI or settings

The intent is that setup is OPTIONAL for Phase 1 testing (workdir can
be pre-seeded) but REQUIRED for a real customer install. The local UI
pair page grows a banner pointing at /setup if anything is missing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .config import AgentSettings, load_settings, save_settings
from .secrets import (
    KEY_CUSTODIAN_ZIP_PASSWORD_PREFIX,
    KEY_M365_CLIENT_SECRET,
    KEY_WS_PORTAL_PASSWORD,
    get_store,
)

# ── Well-known non-secret settings keys (stored in agent-config.json) ── #
# Secrets live in DPAPI; everything else lives in the `extras` dict on
# AgentSettings so we don't keep growing new top-level columns.

EK_M365_TENANT_ID = "m365_tenant_id"
EK_M365_CLIENT_ID = "m365_client_id"
EK_M365_MAILBOX = "m365_mailbox"
EK_WS_USERNAME = "ws_portal_username"


# Custodians whose feeds require a password to decrypt. All four active
# custodians need one today:
#   icici — zip password on the envelope
#   hdfc  — zip password on the envelope
#   kotak — zip password on the envelope
#   axis  — zip is unencrypted, the .xlsx *inside* is AES-256 password
#           protected (msoffcrypto-tool decrypts it in the parser)
# The wizard collects a single password per source — the parser knows
# whether to apply it to the outer zip or the inner file.
CUSTODIANS_WITH_ZIP_PASSWORDS = ("icici", "hdfc", "kotak", "axis")


# ── Step definitions ─────────────────────────────────────────────────── #


@dataclass
class SetupStep:
    key: str
    title: str
    description: str
    completed: Callable[[AgentSettings], bool]


def _workdir_ok(s: AgentSettings) -> bool:
    return bool(s.workdir)


def _m365_ok(s: AgentSettings) -> bool:
    extras = s.extras or {}
    has_fields = all(
        extras.get(k) for k in (EK_M365_TENANT_ID, EK_M365_CLIENT_ID, EK_M365_MAILBOX)
    )
    has_secret = bool(get_store().get(KEY_M365_CLIENT_SECRET))
    return has_fields and has_secret


def _ws_ok(s: AgentSettings) -> bool:
    extras = s.extras or {}
    has_fields = bool(extras.get(EK_WS_USERNAME))
    has_secret = bool(get_store().get(KEY_WS_PORTAL_PASSWORD))
    return has_fields and has_secret


def _custodian_passwords_ok(_s: AgentSettings) -> bool:
    store = get_store()
    return all(
        bool(store.get(KEY_CUSTODIAN_ZIP_PASSWORD_PREFIX + src))
        for src in CUSTODIANS_WITH_ZIP_PASSWORDS
    )


STEPS: list[SetupStep] = [
    SetupStep(
        key="workdir",
        title="Working directory",
        description=(
            "Where the agent reads raw custodian files from and writes "
            "recon reports to. Defaults to %ProgramData%\\Keystone\\workdir "
            "but you can point it anywhere your ops team already stores "
            "these files."
        ),
        completed=_workdir_ok,
    ),
    SetupStep(
        key="m365",
        title="Microsoft 365 / Graph credentials",
        description=(
            "Used to fetch custodian statements from your shared mailbox. "
            "Enter the Azure app registration details for the Graph "
            "permission scopes. Credentials are stored encrypted at rest "
            "via Windows DPAPI."
        ),
        completed=_m365_ok,
    ),
    SetupStep(
        key="ws",
        title="WS portal credentials",
        description=(
            "Used to download Z13 masters, Holdings, TradeTrans, BankBook "
            "and other daily WS exports. The portal password is stored "
            "encrypted at rest via Windows DPAPI."
        ),
        completed=_ws_ok,
    ),
    SetupStep(
        key="custodian_passwords",
        title="Custodian zip passwords",
        description=(
            "HDFC and Axis wrap daily statements in encrypted zips. "
            "Enter each custodian's zip password once; the agent will "
            "use them automatically on every future fetch. Encrypted "
            "at rest via Windows DPAPI."
        ),
        completed=_custodian_passwords_ok,
    ),
]


def setup_state() -> dict[str, Any]:
    """Snapshot of where the operator is in the wizard.

    Returns a dict safe to serialise into the local UI:
      { steps: [{key, title, description, completed}], next_incomplete: 'm365',
        all_done: False, extras: { workdir, m365_tenant_id, ... (non-secret) } }
    """
    settings = load_settings()
    steps_out = []
    next_incomplete: str | None = None
    for step in STEPS:
        done = step.completed(settings)
        steps_out.append(
            {
                "key": step.key,
                "title": step.title,
                "description": step.description,
                "completed": done,
            }
        )
        if not done and next_incomplete is None:
            next_incomplete = step.key
    extras = settings.extras or {}
    return {
        "steps": steps_out,
        "next_incomplete": next_incomplete,
        "all_done": next_incomplete is None,
        "extras": {
            "workdir": settings.workdir,
            EK_M365_TENANT_ID: extras.get(EK_M365_TENANT_ID, ""),
            EK_M365_CLIENT_ID: extras.get(EK_M365_CLIENT_ID, ""),
            EK_M365_MAILBOX: extras.get(EK_M365_MAILBOX, ""),
            EK_WS_USERNAME: extras.get(EK_WS_USERNAME, ""),
        },
        "secret_flags": {
            "m365_client_secret": bool(get_store().get(KEY_M365_CLIENT_SECRET)),
            "ws_portal_password": bool(get_store().get(KEY_WS_PORTAL_PASSWORD)),
            **{
                f"custodian_zip_password_{src}": bool(
                    get_store().get(KEY_CUSTODIAN_ZIP_PASSWORD_PREFIX + src)
                )
                for src in CUSTODIANS_WITH_ZIP_PASSWORDS
            },
        },
    }


# ── Save helpers ─────────────────────────────────────────────────────── #


def save_workdir(path: str) -> None:
    settings = load_settings()
    settings.workdir = path.strip()
    save_settings(settings)


def save_m365(
    *,
    tenant_id: str,
    client_id: str,
    client_secret: str | None,
    mailbox: str,
) -> None:
    """Save the M365 Graph credentials.

    `client_secret` is optional — if blank, we keep whatever is already
    in DPAPI. This lets the wizard re-save the non-secret fields without
    forcing the operator to retype the secret.
    """
    settings = load_settings()
    extras = dict(settings.extras or {})
    extras[EK_M365_TENANT_ID] = tenant_id.strip()
    extras[EK_M365_CLIENT_ID] = client_id.strip()
    extras[EK_M365_MAILBOX] = mailbox.strip()
    settings.extras = extras
    save_settings(settings)
    if client_secret:
        get_store().set(KEY_M365_CLIENT_SECRET, client_secret)


def save_ws_portal(
    *,
    username: str,
    password: str | None,
) -> None:
    settings = load_settings()
    extras = dict(settings.extras or {})
    extras[EK_WS_USERNAME] = username.strip()
    settings.extras = extras
    save_settings(settings)
    if password:
        get_store().set(KEY_WS_PORTAL_PASSWORD, password)


def save_custodian_password(source: str, password: str) -> None:
    if source not in CUSTODIANS_WITH_ZIP_PASSWORDS:
        raise ValueError(f"Unknown custodian: {source}")
    get_store().set(KEY_CUSTODIAN_ZIP_PASSWORD_PREFIX + source, password)
