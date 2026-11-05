"""Small JSON/binary operations executed inside the PVC reader pod. No credentials."""
from __future__ import annotations
import json
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path
from .config import config_from_frozen
from .state import State
from .qualification import require_qualification, write_evidence

DATA = Path('/data')
IMAGE_MANIFEST = Path('/opt/bench/image-manifest.json')


def safe_id(value: str) -> str:
    import re
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,45}', value):
        raise ValueError('invalid experiment ID')
    return value


def status(run_id: str) -> dict:
    run_id = safe_id(run_id)
    state = State(DATA / 'state' / f'{run_id}.db', readonly=True)
    counts = state.counts()
    return {**counts, 'run_id': run_id, 'stage': state.meta('stage'),
            'control': state.meta('control'), 'heartbeat': state.meta('heartbeat'),
            'finished_at': state.meta('finished_at'),
            'storage': json.loads(state.meta('storage', '{}')),
            'dispatch_blocked': state.meta('dispatch_blocked'),
            'active': [dict(x) for x in state.live_attempts()]}


def export_run(run_id: str) -> None:
    run_id = safe_id(run_id)
    state_path = DATA / 'state' / f'{run_id}.db'
    state = State(state_path, readonly=True)
    if state.live_attempts() or state.meta('control') not in ('paused', 'cancelled') and not state.meta('finished_at'):
        raise ValueError('pause and drain the run before exporting; completed runs can be exported directly')
    with tempfile.TemporaryDirectory() as td:
        backup = Path(td) / f'{run_id}.db'
        dst = sqlite3.connect(backup)
        state.db.backup(dst)
        dst.close()
        with tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz') as tf:
            tf.add(DATA / 'runs' / run_id, arcname=f'runs/{run_id}')
            tf.add(backup, arcname=f'state/{run_id}.db')
            q = DATA / 'qualification' / state.meta('config_sha')
            if q.exists():
                tf.add(q, arcname=f'qualification/{q.name}')


def main() -> None:
    req = json.load(sys.stdin)
    op = req['op']
    if op == 'status':
        result = status(req['run_id'])
    elif op == 'gate':
        cfg = config_from_frozen(json.dumps(req['config']))
        result = require_qualification(DATA, cfg)
    elif op == 'image_provenance':
        cfg = config_from_frozen(json.dumps(req['config']))
        try:
            manifest = json.loads(IMAGE_MANIFEST.read_text())
        except (OSError, ValueError) as e:
            raise ValueError('runner image has no valid /opt/bench/image-manifest.json') from e
        if manifest.get('schema') != 1:
            raise ValueError('unsupported runner image manifest schema')
        for harness in cfg.harnesses:
            expected = cfg.data['harnesses'][harness]['version']
            actual = manifest.get('harnesses', {}).get(harness, {}).get('version')
            if actual != expected:
                raise ValueError(f'image provenance mismatch for {harness}: config={expected!r}, image={actual!r}')
        expected_commit = cfg.data['loop']['commit']
        actual_commit = manifest.get('harnesses', {}).get('loop', {}).get('commit')
        if actual_commit != expected_commit:
            raise ValueError(f'image provenance mismatch for Loop commit: config={expected_commit!r}, image={actual_commit!r}')
        result = {'passed': True, 'image_manifest': manifest}
    elif op == 'qualification':
        cfg = config_from_frozen(json.dumps(req['config']))
        try:
            result = json.loads((DATA / 'qualification' / cfg.sha256() / f"{req['stage']}.json").read_text())
        except (OSError, ValueError) as e:
            raise ValueError(f"qualification {req['stage']} produced no evidence") from e
        if result.get('config_sha256') != cfg.sha256():
            raise ValueError(f"qualification {req['stage']} belongs to another configuration")
    elif op == 'evidence':
        cfg = config_from_frozen(json.dumps(req['config']))
        result = write_evidence(DATA, cfg, req['stage'], req['evidence'])
    elif op == 'replace':
        rid = safe_id(req['run_id'])
        s = State(DATA / 'state' / f'{rid}.db')
        if s.meta('control') != 'paused' or s.live_attempts():
            raise ValueError('replacement requires a paused, drained run')
        s.replace_slot(req['slot_id'], req['reason'])
        result = {'replaced_slot': req['slot_id'], 'reason': req['reason']}
    elif op == 'control':
        rid = safe_id(req['run_id'])
        s = State(DATA / 'state' / f'{rid}.db', readonly=True)
        desired = req['state']
        terminal = s.meta('finished_at') is not None or s.meta('control') == 'cancelled'
        if desired == 'running' and terminal:
            raise ValueError('cannot resume a completed or cancelled run; launch a new run')
        if desired not in ('running', 'paused', 'cancelled'):
            raise ValueError('invalid control state')
        result = {'control': s.meta('control'), 'finished_at': s.meta('finished_at'), 'terminal': terminal}
    elif op == 'logs':
        import re, time
        rid = safe_id(req['run_id'])
        aid = req['trial']
        if not re.fullmatch(r'[a-f0-9-]{36}', aid):
            raise ValueError('invalid trial ID')
        root = DATA / 'runs' / rid / 'trials' / aid
        if not root.is_dir():
            raise ValueError('trial not found')
        offsets = {}
        while True:
            for name in ('agent.stdout.log', 'agent.stderr.log'):
                path = root / name
                if not path.exists():
                    continue
                with path.open('rb') as f:
                    if name not in offsets:
                        f.seek(max(0, path.stat().st_size - 16384))
                    else:
                        f.seek(offsets[name])
                    chunk = f.read(65536)
                    offsets[name] = f.tell()
                if chunk:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
            if not req.get('follow') or (root / 'COMPLETE').exists():
                break
            time.sleep(0.5)
        return
    elif op == 'export':
        export_run(req['run_id'])
        return
    else:
        raise ValueError(f'unknown remote operation {op}')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
