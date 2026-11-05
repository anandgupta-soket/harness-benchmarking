"""Local operator CLI. Model calls occur only inside explicitly launched trial Jobs."""
from __future__ import annotations
import argparse
import base64
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .config import Config, ConfigError, config_from_frozen, load_config
from . import manifests as M
from .kube import Kube, KubeError
from .matrix import stage_matrix
from .tasks import TASK_IDS

LOCAL = Path('.bench')
REMOTE_COMMAND = ['python', '-m', 'bench.remote']


def plan(cfg: Config, stage: str = 'scored') -> dict:
    slots = stage_matrix(cfg.data, stage, list(TASK_IDS))
    return {'configuration_sha256': cfg.sha256(), 'stage': stage, 'scored': stage == 'scored',
            'trials': len(slots), 'tasks': sorted({s.task for s in slots}),
            'harnesses': sorted({s.harness for s in slots}), 'profiles_mib': sorted({s.profile_mib for s in slots}),
            'max_concurrent': cfg.get('experiment.max_concurrent_trials'), 'budget': M.audit_budget(cfg),
            'unresolved': cfg.unresolved, 'problems': cfg.problems,
            'reference_note': '2048 MiB is a candidate reference, not proven unconstrained',
            'limits_disclosure': cfg.data['limits_disclosure']}


def reader(k: Kube, cfg: Config) -> str:
    name = 'bench-reader'
    pod = k.get_json('pod', name, check=False)
    if pod:
        labels = pod['metadata'].get('labels', {})
        if labels.get('app.kubernetes.io/name') != 'loop-bench' or labels.get(f'{M.LABEL}/role') != 'reader':
            raise ValueError('bench-reader exists but is not owned by this benchmark')
        spec = pod['spec']
        claim = next((v.get('persistentVolumeClaim', {}).get('claimName') for v in spec.get('volumes', [])
                      if v.get('name') == 'data'), None)
        image = next((c.get('image') for c in spec.get('containers', []) if c.get('name') == 'reader'), None)
        if (spec.get('nodeSelector', {}).get('kubernetes.io/hostname') != cfg.get('kube.worker_node')
                or claim != cfg.get('storage.pvc_name') or image != cfg.get('images.runner')
                or pod.get('status', {}).get('phase') in ('Succeeded', 'Failed')):
            # The benchmark reader has no workload state; replace only the pod we just
            # proved is owned by this operator, so configuration changes cannot use a
            # stale volume, node, or runner image.
            k.run(['delete', 'pod', name, '--wait=true', '--timeout=60s'])
            pod = None
    if not pod:
        k.create(M.reader_pod(cfg, name))
    k.run(['wait', '--for=condition=Ready', f'pod/{name}', '--timeout=180s'], timeout=200)
    return name


def remote(k: Kube, cfg: Config, payload: dict) -> dict:
    p = k.run(['exec', '-i', reader(k, cfg), '--', *REMOTE_COMMAND], stdin=json.dumps(payload).encode(), timeout=180)
    return json.loads(p.stdout)


def remember(cfg_path: str, run_id: str) -> None:
    LOCAL.mkdir(exist_ok=True)
    (LOCAL / 'latest.json').write_text(json.dumps({'config': str(Path(cfg_path).resolve()), 'run_id': run_id}))


def current(args) -> tuple[Config, Kube, str]:
    latest = {}
    if (LOCAL / 'latest.json').exists():
        latest = json.loads((LOCAL / 'latest.json').read_text())
    cfg = load_config(args.config or latest.get('config', 'configs/glm53.yaml'))
    if args.local_kubectl:
        cfg.data['kube']['access'] = 'local-kubectl'
    rid = getattr(args, 'run_id', None) or latest.get('run_id')
    if not rid:
        raise ValueError('provide --run-id or launch an experiment first')
    from .remote import safe_id
    safe_id(rid)
    return cfg, Kube.from_config(cfg), rid


def ensure_idle(k: Kube) -> None:
    jobs = k.get_json('jobs', selector=f'{M.LABEL}/role=controller') or {}
    for job in jobs.get('items', []):
        st = job.get('status', {})
        if not st.get('succeeded') and not any(c.get('type') == 'Failed' and c.get('status') == 'True' for c in st.get('conditions', [])):
            raise ValueError('another controller is active; cancel/finish it before launching another run')


def launch(cfg: Config, k: Kube, stage: str, cfg_path: str) -> str:
    cfg.require_resolved()
    cfg.require_digest_pinned()
    M.audit_budget(cfg)
    if stage == 'scored':
        remote(k, cfg, {'op': 'gate', 'config': cfg.data})
    ensure_idle(k)
    rid = f"{('task' if stage == 'single' else stage)}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    k.create(M.config_map(cfg, rid))
    k.create(M.control_map(cfg, rid))
    k.create(M.controller_job(cfg, rid, stage))
    remember(cfg_path, rid)
    print(f'Experiment: {rid} ({stage}, {len(stage_matrix(cfg.data, stage, list(TASK_IDS)))} trials)')
    print(f'uv run bench status --run-id {rid} --watch')
    print('The controller runs in Kubernetes; disconnecting this terminal does not stop it.')
    return rid


def wait_run(k: Kube, cfg: Config, rid: str) -> dict:
    failures = 0
    while True:
        try:
            d = remote(k, cfg, {'op': 'status', 'run_id': rid})
            failures = 0
        except KubeError:
            # PVC may not be mounted until the first pull finishes. Retry a short
            # transport/readiness outage, but surface a terminal controller Job.
            failures += 1
            try:
                jobs = k.get_json('jobs', selector=f'{M.LABEL}/role=controller') or {}
                controller = next((j for j in jobs.get('items', [])
                                   if j.get('metadata', {}).get('labels', {}).get(f'{M.LABEL}/run') == rid), None)
                st = (controller or {}).get('status', {})
                if st.get('failed') or any(c.get('type') == 'Failed' and c.get('status') == 'True'
                                            for c in st.get('conditions', [])):
                    raise ValueError(f'controller Job for {rid} failed before status became available')
            except KubeError:
                pass
            if failures >= 12:
                raise ValueError(f'cannot reach run status for {rid} after repeated retries; inspect controller logs')
            time.sleep(min(5 * failures, 20))
            continue
        if d.get('finished_at') or d.get('control') == 'cancelled':
            return d
        print(json.dumps({'run_id': rid, 'outcomes': d['outcomes'], 'active': len(d['active'])}), flush=True)
        time.sleep(10)


def qualification_job(k: Kube, cfg: Config, stage: str) -> dict:
    ensure_idle(k)
    evidence = {'passed': True, 'probes': [], 'reasons': []}
    profiles = cfg.profiles if stage == 'resources' else [4096]
    for memory in profiles:
        name = f'bench-{stage}-{uuid.uuid4().hex[:12]}'
        obj = M.probe_job(cfg, name, memory, name)
        if stage == 'controls':
            pod = obj['spec']['template']['spec']
            pod['containers'][0]['command'] = ['python', '-c', "import runpy; from bench.paths import REPO_ROOT; runpy.run_path(str(REPO_ROOT/'controls/run_controls.py'), run_name='__main__')"]
            pod['containers'][0]['resources'] = M._res(2, 4096, 8)
            obj['spec']['activeDeadlineSeconds'] = 900
        k.create(obj)
        try:
            deadline = time.monotonic() + (960 if stage == 'controls' else 360)
            job = None
            while time.monotonic() < deadline:
                job = k.get_json('job', name)
                st = job.get('status', {})
                if st.get('succeeded') or st.get('failed') or any(c.get('type') == 'Failed' and c.get('status') == 'True' for c in st.get('conditions', [])):
                    break
                time.sleep(3)
            pods = k.pods_for(f'job-name={name}')
            logs = k.logs(pods[0]['metadata']['name']) if pods else ''
            if stage == 'resources':
                lines = [l.split('BENCH_PROBE_RESULT ', 1)[1] for l in logs.splitlines() if l.startswith('BENCH_PROBE_RESULT ')]
                result = json.loads(lines[-1]) if lines else {'ok': False, 'reasons': ['no cgroup probe result']}
                good = result.get('ok')
            else:
                try:
                    result = json.loads(logs)
                    good = len(result) == 13 and all(x.get('positive') and x.get('negative_rejected') for x in result.values())
                except ValueError:
                    result, good = {'error': 'control job produced no valid JSON', 'log_tail': logs[-3000:]}, False
            good = good and bool(job and job.get('status', {}).get('succeeded'))
            evidence['probes'].append({'memory_mib': memory, 'passed': bool(good), 'result': result})
            if not good:
                evidence['passed'] = False
                evidence['reasons'].append(f'{stage} at {memory} MiB failed')
        finally:
            k.delete('job', name)
    return remote(k, cfg, {'op': 'evidence', 'config': cfg.data, 'stage': stage, 'evidence': evidence})


def secret_keys(k: Kube, name: str) -> list[str] | None:
    # Return only field NAMES; never transfer a stored credential to this machine.
    template = '{{range $key,$value := .data}}{{$key}}{{"\\n"}}{{end}}'
    p = k.run(['get', 'secret', name, '-o', 'go-template=' + template], check=False)
    if p.returncode:
        if b'NotFound' in (p.stderr or b''):
            return None
        raise ValueError('cannot inspect API-key Secret metadata')
    return p.stdout.decode().splitlines()


def setup(args) -> int:
    cfg = load_config(args.config or 'configs/glm53.yaml')
    if args.local_kubectl:
        cfg.data['kube']['access'] = 'local-kubectl'
    cfg.require_resolved()
    k = Kube.from_config(cfg)
    M.audit_budget(cfg)
    if args.api_key_env:
        value = os.environ.get(args.api_key_env)
        if not value:
            raise ValueError(f'environment variable {args.api_key_env} is empty')
        sec = cfg.data['model']['api_key_secret']
        secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': sec['name'], 'namespace': cfg.namespace},
                  'type': 'Opaque', 'data': {sec['key']: base64.b64encode(value.encode()).decode()}}
        existing = secret_keys(k, sec['name'])
        if existing is not None and not args.replace_api_key:
            raise ValueError('Secret exists; use --replace-api-key to replace its configured key explicitly')
        if existing is not None:
            # Patch one key only; preserve any unrelated secret data.
            k.run(['patch', 'secret', sec['name'], '--type=merge', '--patch-file=/dev/stdin'],
                  stdin=json.dumps({'data': secret['data']}).encode())
        else:
            k.create(secret)
    else:
        sec = cfg.data['model']['api_key_secret']
        keys = secret_keys(k, sec['name'])
        if keys is None or sec['key'] not in keys:
            raise ValueError('configured API-key Secret/key missing; use setup --api-key-env ENV_NAME')
    print(k.apply(M.setup_objects(cfg, args.network_policy)))
    reader(k, cfg)  # binds the local-path PVC on the configured worker
    remote(k, cfg, {'op': 'image_provenance', 'config': cfg.data})
    print('Setup ready. No model calls or benchmark trials were launched.')
    return 0


def export(args) -> int:
    cfg, k, rid = current(args)
    outdir = Path(args.output)
    outdir.mkdir(parents=True, exist_ok=True)
    dest = outdir / f'{rid}.tar.gz'
    if dest.exists():
        raise ValueError(f'refusing to overwrite {dest}')
    pods = k.pods_for(f'{M.LABEL}/run={rid}')
    if any(p.get('metadata', {}).get('labels', {}).get(f'{M.LABEL}/role') in ('trial', 'verifier') and p.get('status', {}).get('phase') not in ('Succeeded', 'Failed') for p in pods):
        raise ValueError('trial/verifier pods are still terminating; wait before exporting')
    helper = reader(k, cfg)
    tmp = dest.with_suffix('.partial')
    try:
        with tmp.open('wb') as f:
            p = k.popen(['exec', '-i', helper, '--', *REMOTE_COMMAND], stdin=subprocess.PIPE, stdout=f, stderr=subprocess.PIPE)
            _, err = p.communicate(json.dumps({'op': 'export', 'run_id': rid}).encode(), timeout=600)
            if p.returncode:
                raise ValueError(err.decode(errors='replace')[-2000:])
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    digest = hashlib.sha256()
    with dest.open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            digest.update(block)
    dest.with_suffix(dest.suffix + '.sha256').write_text(f'{digest.hexdigest()}  {dest.name}\n')
    print(dest)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--config', default=None)
    common.add_argument('--local-kubectl', action='store_true')
    common.add_argument('--run-id', default=None)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan', parents=[common]); p.add_argument('--stage', default='scored', choices=['scored', 'protocol', 'matrix']); p.add_argument('--manifests', action='store_true')
    p = sub.add_parser('setup', parents=[common]); p.add_argument('--api-key-env'); p.add_argument('--replace-api-key', action='store_true'); p.add_argument('--network-policy', action='store_true')
    sub.add_parser('run', parents=[common])
    p = sub.add_parser('qualify', parents=[common]); p.add_argument('--stage', choices=['all', 'resources', 'controls', 'protocol', 'matrix'], default='all')
    p = sub.add_parser('task', parents=[common]); p.add_argument('task', choices=TASK_IDS); p.add_argument('--harness', required=True); p.add_argument('--cpu', type=float, choices=[1.0], default=1.0); p.add_argument('--memory', default='512Mi')
    p = sub.add_parser('status', parents=[common]); p.add_argument('--watch', action='store_true')
    p = sub.add_parser('logs', parents=[common]); p.add_argument('--trial', required=True); p.add_argument('--follow', action='store_true')
    for command in ['pause', 'resume', 'cancel']:
        sub.add_parser(command, parents=[common])
    p = sub.add_parser('replace', parents=[common]); p.add_argument('--slot', required=True); p.add_argument('--reason', required=True)
    p = sub.add_parser('export', parents=[common]); p.add_argument('--output', default='exports')
    p = sub.add_parser('report'); p.add_argument('--input', required=True); p.add_argument('--output', default='report')
    p = sub.add_parser('image'); si = p.add_subparsers(dest='image_command', required=True); q = si.add_parser('resolve'); q.add_argument('--image', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'image':
            proc = subprocess.run(['docker', 'buildx', 'imagetools', 'inspect', args.image, '--format', '{{json .Manifest.Digest}}'], capture_output=True, text=True, check=True)
            digest = json.loads(proc.stdout)
            if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
                raise ValueError('registry returned an invalid image digest')
            print(args.image.split('@')[0] + '@' + digest)
            return 0
        if args.command == 'report':
            from .report import generate_report
            source = Path(args.input)
            if source.is_file():
                import tempfile, tarfile, shutil
                checksum = source.with_suffix(source.suffix + '.sha256')
                if checksum.exists():
                    fields = checksum.read_text().strip().split()
                    if len(fields) != 2 or fields[1] != source.name or not re.fullmatch(r'[a-f0-9]{64}', fields[0]):
                        raise ValueError('invalid export SHA-256 sidecar')
                    if hashlib.sha256(source.read_bytes()).hexdigest() != fields[0]:
                        raise ValueError('export SHA-256 verification failed')
                with tempfile.TemporaryDirectory(prefix='bench-report-') as td:
                    root = Path(td).resolve()
                    with tarfile.open(source) as archive:
                        for member in archive:
                            dest = (root / member.name).resolve()
                            if not dest.is_relative_to(root) or not (member.isdir() or member.isfile()):
                                raise ValueError('unsafe export archive member')
                            if member.isdir():
                                dest.mkdir(parents=True, exist_ok=True)
                            else:
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                with dest.open('xb') as f:
                                    shutil.copyfileobj(archive.extractfile(member), f)
                    result = generate_report(root, Path(args.output))
            else:
                result = generate_report(source, Path(args.output))
            print(json.dumps(result, indent=2))
            return 0
        if args.command == 'setup':
            return setup(args)
        if args.command in ('plan', 'run', 'qualify', 'task'):
            cfg = load_config(args.config or 'configs/glm53.yaml')
            if args.local_kubectl:
                cfg.data['kube']['access'] = 'local-kubectl'
            if args.command == 'plan':
                result = plan(cfg, args.stage)
                if args.manifests:
                    result['manifests'] = M.setup_objects(cfg) + [M.config_map(cfg, 'preview'), M.controller_job(cfg, 'preview', args.stage)]
                print(json.dumps(result, indent=2))
                return 2 if cfg.problems else 0
            cfg.require_resolved(); cfg.require_digest_pinned()
            k = Kube.from_config(cfg)
            if args.command in ('run', 'qualify', 'task'):
                remote(k, cfg, {'op': 'image_provenance', 'config': cfg.data})
            if args.command == 'task':
                if args.harness not in cfg.harnesses:
                    raise ValueError('harness must be one of the configured harnesses')
                memory = int(args.memory.removesuffix('Mi'))
                d = copy.deepcopy(cfg.data)
                d['experiment'].update(repetitions=1, memory_mib=[memory])
                d['matrix'] = {'harnesses': [args.harness]}; d['single_trial'] = {'task': args.task}
                cfg = config_from_frozen(json.dumps(d)); cfg.require_resolved()
                launch(cfg, k, 'single', args.config or 'configs/glm53.yaml')
            elif args.command == 'run':
                launch(cfg, k, 'scored', args.config or 'configs/glm53.yaml')
            else:
                stages = ['resources', 'controls', 'protocol', 'matrix'] if args.stage == 'all' else [args.stage]
                for stage in stages:
                    if stage in ('resources', 'controls'):
                        result = qualification_job(k, cfg, stage)
                        print(json.dumps(result, indent=2))
                        if not result['passed']:
                            return 1
                    else:
                        rid = launch(cfg, k, stage, args.config or 'configs/glm53.yaml')
                        wait_run(k, cfg, rid)
                        result = remote(k, cfg, {'op': 'qualification', 'config': cfg.data, 'stage': stage})
                        print(json.dumps(result, indent=2))
                        if not result.get('passed'):
                            return 1
            return 0
        cfg, k, rid = current(args)
        if args.command == 'status':
            while True:
                d = remote(k, cfg, {'op': 'status', 'run_id': rid})
                print(json.dumps(d, indent=2), flush=True)
                if not args.watch or d.get('finished_at') or d.get('control') == 'cancelled':
                    return 0
                time.sleep(5)
        elif args.command in ('pause', 'resume', 'cancel'):
            state = {'pause': 'paused', 'resume': 'running', 'cancel': 'cancelled'}[args.command]
            remote(k, cfg, {'op': 'control', 'run_id': rid, 'state': state})
            k.annotate_cm(M.control_map_name(rid), 'state', state)
            print(f'{rid}: requested {state}; inspect status for acknowledgement')
        elif args.command == 'replace':
            print(json.dumps(remote(k, cfg, {'op': 'replace', 'run_id': rid, 'slot_id': args.slot, 'reason': args.reason})))
        elif args.command == 'export':
            return export(args)
        elif args.command == 'logs':
            pod = reader(k, cfg)
            p = k.popen(['exec', '-i', pod, '--', *REMOTE_COMMAND], stdin=subprocess.PIPE)
            p.communicate(json.dumps({'op': 'logs', 'run_id': rid, 'trial': args.trial, 'follow': args.follow}).encode())
            return p.returncode
        return 0
    except KeyboardInterrupt:
        print('Detached; cluster workloads continue. Use bench cancel to stop them.', file=sys.stderr)
        return 130
    except (ConfigError, KubeError, ValueError, OSError, subprocess.SubprocessError) as e:
        # Do not emit tracebacks or configuration payloads that might contain credentials.
        message = str(e)
        for name in ('BENCH_API_KEY', getattr(args, 'api_key_env', '') or ''):
            secret = os.environ.get(name, '')
            if secret:
                message = message.replace(secret, '[REDACTED]').replace(base64.b64encode(secret.encode()).decode(), '[REDACTED]')
        print(message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
