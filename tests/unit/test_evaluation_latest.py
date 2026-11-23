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
    client = TestClient(main.app)
    with patch.object(evaluation, 'require_workspace_user', side_effect=main.HTTPException(401, 'Sign in.')):
        assert client.get('/api/evaluation/latest', headers=HEADERS).status_code == 401
    with patch.object(evaluation, 'require_workspace_user', return_value={'role': 'accountant'}):
        assert client.get('/api/evaluation/latest', headers=HEADERS).status_code == 404
        write(tmp_path, 'lite-benchmark-20261124T000000Z.json', documents=[{'id': 1}], settings={'manifest_dir': 'holdout'})
        response = client.get('/api/evaluation/latest', headers=HEADERS)
        assert response.status_code == 200 and response.json()['benchmark'] == 'lite-extraction'
