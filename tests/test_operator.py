import copy
import json
from pathlib import Path
import pytest
from bench.cli import main, plan, secret_keys
from bench.config import Config, config_from_frozen, validate
from bench.qualification import require_qualification, write_evidence, REQUIRED
from bench import manifests as M
from bench.controller import Controller
from tests.fakekube import FakeKube, World
from tests.test_controller import make_cfg


def test_plan_never_constructs_a_kubernetes_client(monkeypatch, capsys):
    monkeypatch.setattr('bench.cli.Kube.from_config', lambda *a, **k: pytest.fail('plan touched Kubernetes'))
    assert main(['plan', '--config', 'configs/glm53.yaml']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['trials'] == 2340
    assert result['max_concurrent'] == 4
    assert result['budget']['pods'] <= 8


def test_reader_helper_has_rw_pvc_for_operator_state(tmp_path):
    cfg = make_cfg(('loop',))
    pod = M.reader_pod(cfg)
    volume = next(v for v in pod['spec']['volumes'] if v['name'] == 'data')
    mount = next(v for v in pod['spec']['containers'][0]['volumeMounts'] if v['name'] == 'data')
    assert volume['persistentVolumeClaim']['claimName'] == cfg.get('storage.pvc_name')
    assert not volume['persistentVolumeClaim'].get('readOnly', False)
    assert not mount.get('readOnly', False)


def test_qualification_is_bound_to_configuration_and_all_stages(tmp_path):
    cfg = make_cfg()
    with pytest.raises(ValueError, match='resources missing'):
        require_qualification(tmp_path, cfg)
    for stage in REQUIRED:
        write_evidence(tmp_path, cfg, stage, {'passed': True})
    assert len(require_qualification(tmp_path, cfg)) == 4
    write_evidence(tmp_path, cfg, 'matrix', {'passed': False})
    with pytest.raises(ValueError, match='matrix failed'):
        require_qualification(tmp_path, cfg)
    d = copy.deepcopy(cfg.data)
    d['model']['base_url'] = 'https://another.invalid/v1'
    cfg2 = config_from_frozen(json.dumps(d))
    with pytest.raises(ValueError, match='missing'):
        require_qualification(tmp_path, cfg2)


def test_cancel_leaves_controller_itself_alive(tmp_path):
    cfg, k = make_cfg(('loop',)), FakeKube()
    c = Controller(cfg, k, tmp_path / 'empty-pvc', 'r', 'scored')
    c.init()
    controller = M.controller_job(cfg, 'r')
    k.create(controller)
    slot = c.state.dispatchable(1)[0]
    a = c.state.begin_attempt(slot['slot_id'])
    c.advance(a)
    c.cancel_all()
    assert ('job', controller['metadata']['name']) in k.objs
    assert c.state.counts()['outcomes'] == {'cancelled': 1}


def test_disappeared_verifier_is_not_silently_retried(tmp_path):
    cfg, k = make_cfg(('loop',)), FakeKube()
    c = Controller(cfg, k, tmp_path, 'r', 'scored')
    c.init()
    a = c.state.begin_attempt(c.state.dispatchable(1)[0]['slot_id'])
    aid = a['attempt_id']
    c.advance(a)
    World(k, tmp_path, 'r').tick()
    c.advance(c.state.attempt(aid))  # running -> collecting
    c.advance(c.state.attempt(aid))  # collecting -> verifying
    c.advance(c.state.attempt(aid))  # create verifier
    name = c.state.attempt(aid)['verifier_job']
    k.delete('job', name)
    c.advance(c.state.attempt(aid))
    assert c.state.attempt(aid)['outcome'] == 'infra_error'
    assert k.created_log.count(name) == 1


def test_secret_metadata_only_uses_key_names():
    import subprocess
    from bench.kube import Kube
    calls = []
    def runner(argv, stdin, timeout):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b'api-key\n', b'')
    k = Kube('local-kubectl', 'bench-constrained', runner=runner)
    assert secret_keys(k, 'bench-api-key') == ['api-key']
    assert 'go-template=' in calls[0][-1]
    assert '{{$key}}' in calls[0][-1] and '{{$value}}' not in calls[0][-1]


def test_config_rejects_credentials_and_provider_path_escape():
    d = copy.deepcopy(make_cfg().data)
    d['model']['api_key'] = 'not-a-key-to-print'
    d['model']['provider_id'] = '../../elsewhere'
    p, _ = validate(d)
    assert any('must not contain credentials' in x for x in p)
    assert any('simple provider identifier' in x for x in p)
    assert all('not-a-key-to-print' not in x for x in p)


def test_config_allows_only_the_explicit_internal_http_endpoint():
    d = copy.deepcopy(make_cfg().data)
    d['model']['base_url'] = 'http://172.17.99.145:30000/v1'
    problems, unresolved = validate(d)
    assert not unresolved
    assert not any('base_url' in problem for problem in problems)
    d['model']['base_url'] = 'http://api.soket.ai/v1'
    problems, _ = validate(d)
    assert any('base_url must use https' in problem for problem in problems)


def test_archive_report_rejects_traversal(tmp_path, monkeypatch, capsys):
    import io, tarfile, types, sys
    monkeypatch.setitem(sys.modules, 'bench.report', types.SimpleNamespace(generate_report=lambda *a: {}))
    archive = tmp_path / 'bad.tar.gz'
    with tarfile.open(archive, 'w:gz') as tf:
        item = tarfile.TarInfo('../escape')
        item.size = 1
        tf.addfile(item, io.BytesIO(b'x'))
    assert main(['report', '--input', str(archive), '--output', str(tmp_path / 'report')]) == 2
    assert 'unsafe' in capsys.readouterr().err
    assert not (tmp_path / 'escape').exists()


def test_qualification_matrix_must_cover_every_scored_harness():
    """An all-nine scored run must not be 'qualified' by a 4-harness matrix smoke."""
    from bench.config import HARNESSES, load_config
    data = copy.deepcopy(load_config('configs/glm53.yaml').data)
    data['qualification']['matrix']['harnesses'] = ['loop', 'pi', 'goose', 'opencode']
    problems, _ = validate(data)
    assert any('qualification.matrix.harnesses' in p for p in problems)
    data['qualification']['matrix']['harnesses'] = list(HARNESSES)
    problems, _ = validate(data)
    assert not any('qualification.matrix.harnesses' in p for p in problems)
