"""SC2 effort study: counterbalanced order, honest reduction rule, label-checked quality, no tenant data."""
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

import main  # noqa: F401  (route modules import from main; load it first as the app does)
from services.routes.effort import routes as study

USER = {'id': 'u1', 'tenant_id': 't1', 'role': 'accountant', 'name': 'Tester'}


def session(participant, mode, sample, seconds, order, correct=4, total=4):
    return {'participant': participant, 'mode': mode, 'sampleFile': sample, 'activeSeconds': seconds, 'order': order,
            'correct': correct, 'fieldsTotal': total}


def complete(participant, manual_first, manual_seconds, assisted_seconds):
    manual_set, assisted_set = ('A', 'B') if manual_first else ('B', 'A')
    rows = [session(participant, 'manual', s, manual_seconds / 2, 1 if manual_first else 2) for s in study.SETS[manual_set]]
    rows += [session(participant, 'ai_assisted', s, assisted_seconds / 2, 2 if manual_first else 1) for s in study.SETS[assisted_set]]
    return rows


class OrderTests(unittest.TestCase):
    def test_odd_and_even_participants_alternate_which_mode_comes_first(self):
        self.assertEqual(study._order('P01'), [('manual', 'A'), ('ai_assisted', 'B')])
        self.assertEqual(study._order('P02'), [('ai_assisted', 'A'), ('manual', 'B')])
        self.assertEqual(study._order('p03'), study._order('P01'))


class SummaryTests(unittest.TestCase):
    def test_no_sessions_is_not_measured(self):
        result = study.summarise([])
        self.assertEqual((result['status'], result['reduction'], result['valid']), ('NOT MEASURED', None, False))

    def test_one_participant_or_one_order_is_not_enough(self):
        self.assertEqual(study.summarise(complete('P01', True, 200, 40))['status'], 'NOT MEASURED')
        both_manual_first = complete('P01', True, 200, 40) + complete('P03', True, 200, 40)
        self.assertEqual(study.summarise(both_manual_first)['status'], 'NOT MEASURED')

    def test_paired_counterbalanced_participants_give_the_measured_reduction(self):
        result = study.summarise(complete('P01', True, 200, 50) + complete('P02', False, 100, 30))
        self.assertTrue(result['valid'])
        self.assertEqual(result['reduction'], round((300 - 80) / 300 * 100, 1))
        self.assertEqual(result['status'], 'MET')
        slower = study.summarise(complete('P01', True, 100, 60) + complete('P02', False, 100, 60))
        self.assertEqual(slower['status'], 'NOT MET')

    def test_incomplete_participant_is_not_paired(self):
        rows = complete('P01', True, 200, 50) + complete('P02', False, 100, 30)[:1]
        result = study.summarise(rows)
        self.assertEqual(len(result['paired']), 1)
        self.assertEqual(result['status'], 'NOT MEASURED')


class SessionTests(unittest.TestCase):
    def record(self, **overrides):
        data = study.SessionInput(**{'participant': 'P01', 'sampleFile': 'REC00003', 'mode': 'manual', 'activeSeconds': 40, 'idleSeconds': 3,
                                     'pausedSeconds': 0, 'values': {}, 'editedKeys': [], 'startedAt': '2026-11-11T05:00:00Z',
                                     'completedAt': '2026-11-11T05:01:00Z', **overrides})
        db = MagicMock(); db.__enter__.return_value = db
        with patch.object(study, 'require_workspace_user', return_value=USER), patch.object(study, 'connect', return_value=db):
            return study.record_session(data, MagicMock()), db

    def test_quality_is_checked_against_published_labels_and_stored_as_an_audit_event(self):
        sample = study._samples()['REC00003']
        labels = {f.field_key: (f.normalized_value if f.normalized_value is not None else f.value) for f in study._fields(sample)}
        values = dict(labels, total='0.01')  # One deliberately wrong amount.
        details, db = self.record(values=values)
        self.assertEqual(details['fieldsTotal'], len(labels))
        self.assertEqual(details['correct'], len(labels) - 1)
        self.assertEqual(details['fieldsTyped'], len(labels))
        sql, params = db.execute.call_args.args
        self.assertIn('INSERT INTO audit_logs', sql)
        self.assertEqual(params[4], study.ACTION)

    def test_wrong_mode_for_the_participant_is_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            self.record(mode='ai_assisted')
        self.assertEqual(caught.exception.status_code, 422)

    def test_incomplete_task_is_rejected(self):
        with self.assertRaises(HTTPException):
            self.record(values={})


class StudyInputTests(unittest.TestCase):
    """Hosted servers have no runs/: the committed, hash-pinned snapshot supplies the measured prefill."""

    def tasks(self, participant='P02'):
        with patch.object(study, 'require_workspace_user', return_value=USER):
            return study.study_tasks(MagicMock(), participant)

    def test_committed_snapshot_serves_ai_assisted_tasks_without_runs(self):
        with patch.dict('os.environ', {'EFFORT_STUDY_RUN_DIR': ''}):
            plan = self.tasks()
        self.assertEqual(plan['aiRun'], 'snapshot:saic-eval-parser')
        assisted = [t for t in plan['tasks'] if t['mode'] == 'ai_assisted']
        self.assertEqual(len(assisted), 2)
        self.assertTrue(all(isinstance(t['prefill'], dict) and any(t['prefill'].values()) for t in assisted))

    def test_snapshot_is_public_licensed_and_pinned_to_the_study_files(self):
        import json
        from ai.evaluation import effort_study_inputs as inputs
        text = inputs.SNAPSHOT.read_text(encoding='utf-8')
        self.assertNotRegex(text, r'[A-Za-z]:\\\\|/Users/|/home/')
        snapshot = json.loads(text)
        samples = study._samples()
        self.assertEqual(set(snapshot['documents']), set(inputs.STUDY_FILES))
        for stem, entry in snapshot['documents'].items():
            self.assertEqual(entry['sourceSha256'], samples[stem].source.sha256)
            self.assertTrue(entry['licence'] and entry['licenceReference'])
            self.assertIn(entry['origin'], {'REAL', 'SYNTHETIC'})

    def test_missing_inputs_give_an_explained_503_and_keep_manual_sessions(self):
        from ai.evaluation import effort_study_inputs as inputs
        with patch.object(inputs, 'SNAPSHOT', inputs.ROOT / 'missing-effort-snapshot.json'), \
                patch.dict('os.environ', {'EFFORT_STUDY_RUN_DIR': ''}):
            with self.assertRaises(HTTPException) as caught:
                self.tasks()
            self.assertEqual(caught.exception.status_code, 503)
            self.assertIn('unavailable on this server', caught.exception.detail)
            db = MagicMock(); db.__enter__.return_value = db
            sample = study._samples()['REC00007']
            values = {f.field_key: 'x' for f in study._fields(sample)}
            data = study.SessionInput(participant='P02', sampleFile='REC00003', mode='ai_assisted', activeSeconds=5, idleSeconds=0,
                                      pausedSeconds=0, values=values, editedKeys=[], startedAt='a', completedAt='b')
            with patch.object(study, 'require_workspace_user', return_value=USER), patch.object(study, 'connect', return_value=db):
                with self.assertRaises(HTTPException) as refused:
                    study.record_session(data, MagicMock())
                db.execute.assert_not_called()  # No AI-assisted session is scored against an empty prefill.
                self.assertEqual(refused.exception.status_code, 503)
                manual = study.SessionInput(**{**data.model_dump(), 'sampleFile': 'REC00007', 'mode': 'manual'})
                details = study.record_session(manual, MagicMock())
            self.assertNotIn('aiInputs', details)
            self.assertIn('INSERT INTO audit_logs', db.execute.call_args.args[0])

    def test_snapshot_for_different_source_bytes_is_refused(self):
        import json, tempfile
        from pathlib import Path
        from ai.evaluation import effort_study_inputs as inputs
        snapshot = json.loads(inputs.SNAPSHOT.read_text(encoding='utf-8'))
        snapshot['documents']['REC00003']['sourceSha256'] = '0' * 64
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'snapshot.json'
            path.write_text(json.dumps(snapshot), encoding='utf-8')
            with self.assertRaisesRegex(inputs.StudyUnavailable, 'different REC00003'):
                inputs.prefill_from_snapshot(study._samples(), path)

    def test_configured_run_without_results_is_unavailable(self):
        import tempfile
        from ai.evaluation import effort_study_inputs as inputs
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(inputs.StudyUnavailable, 'summary.jsonl'):
                inputs.study_prefill(study._samples(), {'EFFORT_STUDY_RUN_DIR': folder})


if __name__ == '__main__':
    unittest.main()
