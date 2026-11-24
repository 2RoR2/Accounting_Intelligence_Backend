"""Plan M B7: GET /api/evaluation/latest returns the newest completed lite benchmark, read-only."""
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from services.routes.reporting import evaluation

HEADERS = {'X-Requested-With': 'AccountingIntelligence', 'Origin': 'http://localhost:3000'}


def write(folder, name, **body):
    (folder / name).write_text(json.dumps({'benchmark': 'lite-extraction', 'created_at': '2026-11-23T00:00:00Z',
                                           'overall': {'accuracy': 0.9}, **body}), encoding='utf-8')


def test_latest_completed_result_with_relative_paths(tmp_path, monkeypatch):
    monkeypatch.setenv('EVALUATION_RUNS_DIR', str(tmp_path))
    write(tmp_path, 'lite-benchmark-20261120T000000Z.json', documents=[{'id': 1}],
          settings={'manifest_dir': str(evaluation.ROOT / 'ai/synthetic/output/holdout')})
    write(tmp_path, 'lite-benchmark-20261122T000000Z.json', documents=[{'id': 1}], stopped='interrupted',
          settings={'manifest_dir': 'x'})
    write(tmp_path, 'lite-benchmark-20261123T000000Z.json', documents=[], settings={})
    result = evaluation.latest_result()
    assert result['file'] == 'lite-benchmark-20261120T000000Z.json'
    assert result['settings']['manifest_dir'] == 'ai/synthetic/output/holdout'


def test_endpoint_requires_a_workspace_user_and_404s_without_results(tmp_path, monkeypatch):
    monkeypatch.setenv('EVALUATION_RUNS_DIR', str(tmp_path))
    monkeypatch.setenv('EVALUATION_PUBLISHED_DIR', str(tmp_path / 'published-none'))
    client = TestClient(main.app)
    with patch.object(evaluation, 'require_workspace_user', side_effect=main.HTTPException(401, 'Sign in.')):
        assert client.get('/api/evaluation/latest', headers=HEADERS).status_code == 401
    with patch.object(evaluation, 'require_workspace_user', return_value={'role': 'accountant'}):
        assert client.get('/api/evaluation/latest', headers=HEADERS).status_code == 404
        write(tmp_path, 'lite-benchmark-20261124T000000Z.json', documents=[{'id': 1}], settings={'manifest_dir': 'holdout'})
        response = client.get('/api/evaluation/latest', headers=HEADERS)
        assert response.status_code == 200 and response.json()['benchmark'] == 'lite-extraction'


def test_published_report_is_the_fallback_when_no_local_run_exists(tmp_path, monkeypatch):
    runs, published = tmp_path / 'runs', tmp_path / 'published'
    runs.mkdir(), published.mkdir()
    monkeypatch.setenv('EVALUATION_RUNS_DIR', str(runs))
    monkeypatch.setenv('EVALUATION_PUBLISHED_DIR', str(published))
    write(published, 'lite-benchmark-20261124T010000Z.json', documents=[{'id': 1}], settings={'manifest_dir': 'holdout'})
    assert evaluation.latest_result()['file'] == 'lite-benchmark-20261124T010000Z.json'
    write(runs, 'lite-benchmark-20261101T000000Z.json', documents=[{'id': 1}], settings={})
    assert evaluation.latest_result()['file'] == 'lite-benchmark-20261101T000000Z.json'  # a local run wins


def _no_machine_paths(text):
    return 'Users' not in text and ':\\' not in text and '/home/' not in text


def test_every_published_report_is_complete_labelled_and_has_no_machine_paths():
    for path in sorted((evaluation.ROOT / 'evaluation').glob('lite-benchmark-*.json')):
        text = path.read_text(encoding='utf-8')
        report = json.loads(text)
        assert report['documents'] and not report.get('stopped'), path.name
        assert report['dataset']['synthetic'] is True and 'synthetic' in report['dataset']['label'].lower()
        assert _no_machine_paths(text), path.name


def test_publish_strips_machine_paths_and_refuses_a_stopped_run(tmp_path):
    from scripts.publish_evaluation import publish
    source = tmp_path / 'run.json'
    body = {'benchmark': 'lite-extraction', 'created_at': '2026-11-24T01:02:03+00:00', 'documents': [{'file': 'HLD-1'}],
            'settings': {'manifest_dir': r'C:\Users\someone\repo\ai\synthetic\output\holdout'},
            'note': '/home/someone/x/report.json', 'stopped': None}
    source.write_text(json.dumps(body), encoding='utf-8')
    target = publish(source, folder=tmp_path / 'evaluation')
    assert target.name == 'lite-benchmark-20261124T010203Z.json'
    text = target.read_text(encoding='utf-8')
    published = json.loads(text)
    assert published['settings']['manifest_dir'] == 'ai/synthetic/output/holdout' and published['note'] == 'report.json'
    assert published['dataset']['synthetic'] is True and _no_machine_paths(text)
    source.write_text(json.dumps({**body, 'stopped': 'PROVIDER_ERROR AI_RATE_LIMITED'}), encoding='utf-8')
    import pytest
    with pytest.raises(ValueError):
        publish(source, folder=tmp_path / 'evaluation')
