"""Routing tests for the extraction stages, run through both runners with stubbed stages."""
import importlib.util
import unittest

from services.extraction.workflow import extraction_graph as graph
from services.extraction.workflow.document_extraction_service import recoverable_fields, usable_candidates

HAS_LANGGRAPH = importlib.util.find_spec('langgraph') is not None
BASE = ['verify_source', 'primary_extract', 'classify_and_map', 'assess_field_evidence', 'select_recovery']


class StubService:
    """Records stage order. needs_retry says how many prechecks ask for a retry."""
    def __init__(self, recovery, needs_retry=0):
        self.recovery, self.needs_retry = recovery, needs_retry

    def _record_stage(self, context, name, started, **details):
        context['summary'].setdefault('stage_trace', []).append(name)

    def stage_verify_source(self, context): pass
    def stage_primary_extract(self, context): pass
    def stage_classify_and_map(self, context): pass

    def stage_assess_field_evidence(self, context):
        context['decision'] = {'decision': 'human_review'}
        context['assessment'] = {'decision': 'X', 'policy_version': 'p', 'recovery_fields': []}

    def stage_select_recovery(self, context):
        context['recovery'] = {'fields': ['merchant']} if self.recovery else None

    def stage_run_qwen_recovery(self, context): pass
    def stage_reconcile_candidates(self, context): pass

    def stage_precheck_validation(self, context):
        context['summary']['validation_precheck'] = {'status': 'findings', 'findings': []}
        context['retry_fields'] = ['merchant'] if self.needs_retry > 0 else []
        self.needs_retry -= 1

    def stage_targeted_retry(self, context):
        context['retries_used'] = context.get('retries_used', 0) + 1

    def stage_final_route(self, context):
        context['summary']['final_route'] = 'ACCOUNTANT_ACTION_REQUIRED'


def runners():
    yield 'plain', graph.run_plain
    if HAS_LANGGRAPH:
        yield 'langgraph', lambda service, context: graph.build_graph(service).invoke(
            {'version': graph.GRAPH_VERSION, 'context': context, 'error': None})


class RoutingTests(unittest.TestCase):
    def trace(self, run, service):
        context = {'summary': {}}
        run(service, context)
        return context['summary']['stage_trace']

    def test_no_recovery_still_reaches_validation_and_final_route(self):
        for name, run in runners():
            with self.subTest(runner=name):
                self.assertEqual(self.trace(run, StubService(False)), BASE + ['precheck_validation', 'final_route'])

    def test_recovery_runs_before_validation(self):
        for name, run in runners():
            with self.subTest(runner=name):
                self.assertEqual(self.trace(run, StubService(True)), BASE + [
                    'run_qwen_recovery', 'reconcile_candidates', 'precheck_validation', 'final_route'])

    def test_one_retry_returns_through_verification_and_validation(self):
        once = ['run_qwen_recovery', 'reconcile_candidates', 'precheck_validation']
        for name, run in runners():
            with self.subTest(runner=name):
                self.assertEqual(self.trace(run, StubService(True, needs_retry=1)),
                                 BASE + once + ['targeted_retry'] + once + ['final_route'])


class RecoverableFieldTests(unittest.TestCase):
    def summary(self, reconciled, findings=('total',), asked=('total_amount', 'merchant')):
        return {'selective_recovery': {'requested_fields': list(asked)},
                'reconciliation': None if reconciled is None else {'fields': reconciled},
                'validation_precheck': {'findings': [{'field': name} for name in findings]}}

    def test_unusable_answer_on_a_needed_field_is_recoverable(self):
        self.assertEqual(recoverable_fields(self.summary(None), 3.0, 'JSONDecodeError'), ['total_amount'])

    def test_timeout_or_unreachable_server_is_not_retried(self):
        for error in ('TimeoutError', 'URLError', None):
            with self.subTest(error=error):
                self.assertEqual(recoverable_fields(self.summary(None), 3.0, error), [])

    def test_unusable_format_on_a_needed_field_is_recoverable(self):
        fields = {'total_amount': {'field_rejection': 'Amounts must be decimal strings'}, 'merchant': {}}
        self.assertEqual(recoverable_fields(self.summary(fields), 3.0), ['total_amount'])

    def test_a_null_answer_is_not_asked_again(self):
        self.assertEqual(recoverable_fields(self.summary({'total_amount': {'field_rejection': None}}), 3.0), [])

    def test_unrelated_finding_does_not_retry(self):
        self.assertEqual(recoverable_fields(self.summary(None, findings=('currency', 'line_items.0.amount')), 3.0, 'ValueError'), [])

    def test_latency_budget_stops_a_retry(self):
        self.assertEqual(recoverable_fields(self.summary(None), 12.1, 'ValueError'), [])

    def test_no_first_attempt_means_no_retry(self):
        self.assertEqual(recoverable_fields({'selective_recovery': None, 'reconciliation': None,
                                             'validation_precheck': {'findings': [{'field': 'total'}]}}, 3.0, 'ValueError'), [])

    def test_usable_candidates_counts_values_only(self):
        fields = {'a': {'vlm': {'candidate': '1.00'}}, 'b': {'vlm': {'candidate': None}}}
        self.assertEqual(usable_candidates({'fields': fields}), 1)
        self.assertEqual(usable_candidates(None), 0)


if __name__ == '__main__':
    unittest.main()
