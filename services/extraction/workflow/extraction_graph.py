"""Run the extraction stages, in order, either plainly or as a LangGraph subworkflow.

Both drive the same stage functions with the same deterministic edges, inside an
already claimed queue job. There is no second queue, no checkpointer and no
persistence: the graph holds references and metadata, not documents or OCR text.
The model never chooses a transition. ENABLE_EXTRACTION_GRAPH selects the graph;
if LangGraph is not installed, the plain runner is used and the reason is recorded.
"""
import os
import time
from typing import Any, Optional, TypedDict

GRAPH_VERSION = 'extraction-graph/1'


def extraction_graph_enabled():
    return os.environ.get('ENABLE_EXTRACTION_GRAPH', '').strip().lower() == 'true'


def _stage(service, name, method):
    def node(state):
        context = state['context']
        started = time.perf_counter()
        getattr(service, method)(context)
        details = {}
        if name == 'assess_field_evidence':
            details = {'route': context['decision']['decision'], 'assessment': context['assessment']['decision'],
                       'policy_version': context['assessment'].get('policy_version'),
                       'recovery_fields': context['assessment'].get('recovery_fields', [])}
        elif name == 'select_recovery':
            details = {'recovery': bool(context.get('recovery'))}
        elif name == 'precheck_validation':
            details = {'status': context['summary']['validation_precheck']['status'],
                       'retry_fields': context.get('retry_fields') or []}
        elif name == 'final_route':
            details = {'route': context['summary']['final_route']}
        service._record_stage(context, name, started, **details)
        return {}
    return node


STAGES = [('verify_source', 'stage_verify_source'), ('primary_extract', 'stage_primary_extract'),
          ('classify_and_map', 'stage_classify_and_map'), ('assess_field_evidence', 'stage_assess_field_evidence'),
          ('select_recovery', 'stage_select_recovery')]
RECOVERY_STAGES = [('run_qwen_recovery', 'stage_run_qwen_recovery'), ('reconcile_candidates', 'stage_reconcile_candidates')]
PRECHECK = ('precheck_validation', 'stage_precheck_validation')
RETRY = ('targeted_retry', 'stage_targeted_retry')
FINAL = ('final_route', 'stage_final_route')


def recovery_edge(state):
    """Deterministic: only the assessment and its permissions decide this."""
    return 'run_qwen_recovery' if state['context'].get('recovery') else 'precheck_validation'


def retry_edge(state):
    """Deterministic: the precheck names fields only once, so this cannot loop."""
    return 'targeted_retry' if state['context'].get('retry_fields') else 'final_route'


class GraphState(TypedDict):
    version: str
    context: Any  # In-process references for this one claimed job; never persisted.
    error: Optional[str]


def build_graph(service):
    from langgraph.graph import END, START, StateGraph
    graph = StateGraph(GraphState)
    for name, method in STAGES + RECOVERY_STAGES + [PRECHECK, RETRY, FINAL]:
        graph.add_node(name, _stage(service, name, method))
    graph.add_edge(START, STAGES[0][0])
    for (first, _), (second, _) in zip(STAGES, STAGES[1:]):
        graph.add_edge(first, second)
    graph.add_conditional_edges('select_recovery', recovery_edge,
                                {'run_qwen_recovery': 'run_qwen_recovery', 'precheck_validation': 'precheck_validation'})
    graph.add_edge('run_qwen_recovery', 'reconcile_candidates')
    graph.add_edge('reconcile_candidates', 'precheck_validation')
    graph.add_conditional_edges('precheck_validation', retry_edge,
                                {'targeted_retry': 'targeted_retry', 'final_route': 'final_route'})
    graph.add_edge('targeted_retry', 'run_qwen_recovery')
    graph.add_edge('final_route', END)
    return graph.compile()


def run_plain(service, context):
    state = {'context': context}
    def step(pair):
        _stage(service, *pair)(state)
    for pair in STAGES:
        step(pair)
    if recovery_edge(state) == 'run_qwen_recovery':
        for pair in RECOVERY_STAGES:
            step(pair)
    step(PRECHECK)
    if retry_edge(state) == 'targeted_retry':
        step(RETRY)
        for pair in RECOVERY_STAGES:
            step(pair)
        step(PRECHECK)
    step(FINAL)


def run_stages(service, context):
    summary = context['summary']
    if extraction_graph_enabled():
        try:
            graph = build_graph(service)
        except ImportError as error:
            summary['orchestration'] = {'runner': 'plain', 'reason': 'LANGGRAPH_NOT_INSTALLED:' + str(error.name)}
            run_plain(service, context)
            return
        summary['orchestration'] = {'runner': 'langgraph', 'version': GRAPH_VERSION}
        graph.invoke({'version': GRAPH_VERSION, 'context': context, 'error': None})
        return
    summary['orchestration'] = {'runner': 'plain'}
    run_plain(service, context)
