"""
core/alias_migration.py — one-shot migration of legacy broker_cn_aliases
from pools_hub.json into broker_map.json broker.pool_aliases.

Historical state: aliases used to live per-pool under
``pools_hub.pools[i].broker_cn_aliases`` with shape
``[{mapin, note, source}, ...]`` where ``source`` was either a dealer_code
('EMKY', 'ESSI') or a descriptive label ('icici_backoffice',
'motilal_custodian', 'haitong').

Target state: aliases live per-broker under
``broker_map.brokers[i].pool_aliases`` with shape
``[{pool_id, alias_code, note}, ...]``.

This module:
  - reads both files
  - for each remaining broker_cn_aliases entry, locates the target broker
    via dealer_code (with a few descriptive-source fallbacks) and either
    confirms it already exists in broker_map.json or appends it
  - rewrites both files atomically; pools_hub.json loses the field once
    every entry has been migrated for that pool

Idempotent: re-running on a clean state is a no-op.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


# Descriptive `source` strings observed in legacy data → broker dealer_code.
# Anything not in this map is tried verbatim against dealer_code (case-insensitive)
# before being flagged unresolved.
_DESCRIPTIVE_SOURCE_MAP = {
    'icici_backoffice': 'ICFX',
    'motilal_custodian': 'MOTI',
    'haitong':           'ESSI',
}


@dataclass
class MigrationReport:
    migrated: List[dict] = field(default_factory=list)        # newly added to broker_map
    duplicates: List[dict] = field(default_factory=list)      # already present in broker_map → dropped from pools_hub
    unresolved: List[dict] = field(default_factory=list)      # source could not be mapped to a broker → kept in pools_hub
    pools_cleared: List[str] = field(default_factory=list)    # pool_ids whose broker_cn_aliases is now empty
    pools_partial: List[str] = field(default_factory=list)    # pool_ids that still have unresolved entries

    @property
    def applied(self) -> bool:
        return bool(self.migrated or self.duplicates)

    def to_dict(self) -> dict:
        return {
            'migrated':       self.migrated,
            'duplicates':     self.duplicates,
            'unresolved':     self.unresolved,
            'pools_cleared':  self.pools_cleared,
            'pools_partial':  self.pools_partial,
            'applied':        self.applied,
        }


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _resolve_broker(brokers: List[dict], source: str) -> Optional[dict]:
    if not source:
        return None
    src_norm = source.strip()
    src_upper = src_norm.upper()
    mapped = _DESCRIPTIVE_SOURCE_MAP.get(src_norm.lower())
    if mapped:
        src_upper = mapped.upper()
    for b in brokers:
        if (b.get('dealer_code') or '').strip().upper() == src_upper:
            return b
    return None


def _alias_already_present(broker: dict, pool_id: str, alias_code: str) -> bool:
    for a in broker.get('pool_aliases', []) or []:
        if ((a.get('pool_id') or '').strip() == pool_id
                and (a.get('alias_code') or '').strip().upper() == alias_code.upper()):
            return True
    return False


def migrate_broker_cn_aliases(hub_path: Path, broker_map_path: Path,
                              dry_run: bool = False) -> MigrationReport:
    """Migrate any leftover ``broker_cn_aliases`` entries.

    Returns a report. With ``dry_run=True`` no files are written.
    """
    report = MigrationReport()
    if not hub_path.exists() or not broker_map_path.exists():
        return report

    hub  = json.loads(hub_path.read_text(encoding='utf-8'))
    bmap = json.loads(broker_map_path.read_text(encoding='utf-8'))

    pools = hub.get('pools', []) or []
    brokers = bmap.get('brokers', []) or []

    # Quick sanity: nothing to do?
    has_any = any(p.get('broker_cn_aliases') for p in pools)
    if not has_any:
        return report

    for pool in pools:
        legacy = pool.get('broker_cn_aliases') or []
        if not legacy:
            # Even if empty, drop the field so the schema converges.
            if 'broker_cn_aliases' in pool:
                pool.pop('broker_cn_aliases', None)
            continue

        pool_id = (pool.get('pool_id') or '').strip()
        kept: List[dict] = []
        for entry in legacy:
            mapin = (entry.get('mapin') or '').strip()
            note  = (entry.get('note') or '').strip()
            src   = (entry.get('source') or '').strip()
            if not mapin or not pool_id:
                kept.append(entry)
                continue
            broker = _resolve_broker(brokers, src)
            if broker is None:
                report.unresolved.append({
                    'pool_id': pool_id, 'alias_code': mapin,
                    'note': note, 'source': src,
                    'reason': f'no broker matched source={src!r}',
                })
                kept.append(entry)
                continue
            if _alias_already_present(broker, pool_id, mapin):
                report.duplicates.append({
                    'pool_id': pool_id, 'alias_code': mapin,
                    'broker': broker.get('dealer_code'), 'source': src,
                })
                continue
            broker.setdefault('pool_aliases', []).append({
                'pool_id': pool_id,
                'alias_code': mapin,
                'note': note or f'migrated from pools_hub.broker_cn_aliases (source={src})',
            })
            report.migrated.append({
                'pool_id': pool_id, 'alias_code': mapin,
                'broker': broker.get('dealer_code'), 'source': src,
            })

        if kept:
            pool['broker_cn_aliases'] = kept
            report.pools_partial.append(pool_id)
        else:
            pool.pop('broker_cn_aliases', None)
            report.pools_cleared.append(pool_id)

    if dry_run:
        return report

    if report.applied or report.pools_cleared:
        _atomic_write_json(broker_map_path, bmap)
        _atomic_write_json(hub_path, hub)
        logger.info('alias_migration: migrated=%d duplicates=%d unresolved=%d '
                    'pools_cleared=%d pools_partial=%d',
                    len(report.migrated), len(report.duplicates),
                    len(report.unresolved), len(report.pools_cleared),
                    len(report.pools_partial))

    return report


def run_default(config_dir: Optional[Path] = None) -> MigrationReport:
    """Convenience wrapper that picks up the standard config paths."""
    if config_dir is None:
        env = os.environ.get('KEYSTONE_CONFIG_DIR')
        if env:
            config_dir = Path(env)
        else:
            config_dir = Path(__file__).parent.parent / 'config'
    return migrate_broker_cn_aliases(
        hub_path=config_dir / 'pools_hub.json',
        broker_map_path=config_dir / 'broker_map.json',
    )


if __name__ == '__main__':  # pragma: no cover
    import argparse
    parser = argparse.ArgumentParser(description='Migrate broker_cn_aliases → broker_map.pool_aliases')
    parser.add_argument('--config-dir', default=None)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    cfg = Path(args.config_dir) if args.config_dir else None
    if args.dry_run:
        rep = migrate_broker_cn_aliases(
            hub_path=(cfg or Path(__file__).parent.parent / 'config') / 'pools_hub.json',
            broker_map_path=(cfg or Path(__file__).parent.parent / 'config') / 'broker_map.json',
            dry_run=True,
        )
    else:
        rep = run_default(cfg)
    print(json.dumps(rep.to_dict(), indent=2))
