"""One-shot migration: move pools.broker_cn_aliases → brokers.pool_aliases.

Before this script, aliases lived on each pool as:
    pool.broker_cn_aliases = [{mapin, source, note}, ...]
where `source` was a loose string like "haitong" / "icici_backoffice".

After, each broker owns a flat list:
    broker.pool_aliases = [{pool_id, alias_code, note}, ...]

Source-string → broker-dealer-code mapping (reviewed with the user):
    EMKY                → EMKY   (Emkay Global)
    ESSI                → ESSI   (Haitong Securities, new dealer_code)
    haitong             → ESSI   (Haitong Securities, old free-text name)
    icici_backoffice    → ICFX   (ICICI Securities — NOT ICICI Bank)
    motilal_custodian   → MOTI   (Motilal Oswal — NOT a custodian)

Usage:
    py -3.12 migrate_aliases.py --dry-run
    py -3.12 migrate_aliases.py
    py -3.12 migrate_aliases.py --config-dir C:\\ProgramData\\Keystone\\config

Backs up both JSON files to *.bak-<epoch> before writing.
Does NOT delete pool.broker_cn_aliases — leaves them as-is for safety.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

SOURCE_TO_DEALER_CODE = {
    "EMKY": "EMKY",
    "ESSI": "ESSI",
    "haitong": "ESSI",
    "icici_backoffice": "ICFX",
    "motilal_custodian": "MOTI",
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def migrate(config_dir: Path, dry_run: bool) -> int:
    pools_path = config_dir / "pools_hub.json"
    broker_path = config_dir / "broker_map.json"

    if not pools_path.exists():
        print(f"ERROR: {pools_path} does not exist", file=sys.stderr)
        return 2
    if not broker_path.exists():
        print(f"ERROR: {broker_path} does not exist", file=sys.stderr)
        return 2

    hub = _load(pools_path)
    bm = _load(broker_path)

    brokers_by_dc = {b.get("dealer_code", "").upper(): b for b in bm.get("brokers", [])}

    moved = 0
    skipped_unknown = []
    unknown_pool_ids = []

    for pool in hub.get("pools", []):
        pool_id = pool.get("pool_id", "")
        aliases = pool.get("broker_cn_aliases", []) or []
        for alias in aliases:
            src = (alias.get("source") or "").strip()
            code = (alias.get("mapin") or "").strip()
            note = (alias.get("note") or "").strip()
            if not src or not code:
                continue
            dc = SOURCE_TO_DEALER_CODE.get(src)
            if dc is None:
                skipped_unknown.append((pool_id, src, code))
                continue
            broker = brokers_by_dc.get(dc.upper())
            if broker is None:
                unknown_pool_ids.append((pool_id, src, code, dc))
                continue
            pa_list = broker.setdefault("pool_aliases", [])
            # Dedupe: skip if we already have this (pool_id, alias_code) pair
            exists = any(
                (pa.get("pool_id") == pool_id and (pa.get("alias_code") or "").strip() == code)
                for pa in pa_list
            )
            if exists:
                continue
            pa_list.append({
                "pool_id": pool_id,
                "alias_code": code,
                "note": note,
            })
            moved += 1
            print(f"  + {dc}.pool_aliases += {{pool_id={pool_id}, alias_code={code}, note={note!r}}}")

    print()
    print(f"Moved: {moved} alias(es)")
    if skipped_unknown:
        print(f"Skipped (unknown source): {len(skipped_unknown)}")
        for p, s, c in skipped_unknown:
            print(f"  ?? pool={p} source={s!r} code={c}")
    if unknown_pool_ids:
        print(f"Skipped (no broker for mapped dealer_code): {len(unknown_pool_ids)}")
        for p, s, c, dc in unknown_pool_ids:
            print(f"  ?? pool={p} source={s!r} code={c} dealer_code={dc}")

    if dry_run:
        print("\n[dry-run] No files written.")
        return 0

    ts = int(time.time())
    shutil.copy2(broker_path, broker_path.with_suffix(f".json.bak-{ts}"))
    _save(broker_path, bm)
    print(f"\nWrote {broker_path} (backup: {broker_path.name}.bak-{ts})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--config-dir",
        default=str(Path("C:/ProgramData/Keystone/config")),
        help="Directory holding pools_hub.json + broker_map.json",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return migrate(Path(args.config_dir), args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
