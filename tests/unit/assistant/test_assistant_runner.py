"""G1 read-only runner: the first statement is READ ONLY, a failure discards its facts, results are capped and masked."""
import json
from dataclasses import replace

import pytest
from assistant_fakes import TODAY, USER, FakeConnect

from services.assistant.facts import FactBook
from services.assistant.runner import (
    READ_ONLY,
    ToolFailed,
    execute,
    model_payload,
    parse_call,
)
from services.assistant.tools import TOOLS, ToolContext
from services.extraction.lite.privacy import Masker


def _ctx():
    return ToolContext(user=USER, today=TODAY, facts=FactBook(USER['role']))


def test_each_tool_runs_in_a_read_only_transaction(fake_tools):
    connect = FakeConnect()
    ctx = _ctx()
    tool, args = parse_call('due_soon', {'kind': 'bills'})
    ids = execute(tool, args, ctx, connect_fn=connect)
    assert connect.store['statements'][0] == READ_ONLY
    assert ids == ['f1', 'f2']
    assert fake_tools[0][2] == USER['tenant_id']


def test_a_write_inside_a_tool_fails_and_its_facts_are_discarded():
    class ReadOnlyDB:
        def __init__(self):
            self.read_only = False

        def execute(self, sql, params=None):
            if sql == READ_ONLY:
                self.read_only = True
                return self
            if self.read_only and sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')):
                raise RuntimeError('cannot execute INSERT in a read-only transaction')
            return self

    class Connect:
        def __enter__(self):
            return ReadOnlyDB()

        def __exit__(self, *exc):
            return False

    def writer(db, ctx, args):
        ctx.facts.add('partial', 1, 'count', source='x')
        db.execute("INSERT INTO audit_logs(action) VALUES ('evil')")

    tool = replace(TOOLS['document_queue'], run=writer)
    ctx = _ctx()
    with pytest.raises(ToolFailed) as error:
        execute(tool, tool.args(), ctx, connect_fn=Connect)
    assert error.value.code == 'TOOL_FAILED'
    assert ctx.facts.items == {}


def test_payload_wraps_masks_and_caps_results():
    ctx = _ctx()
    ids = [ctx.facts.add(f'Contact {n} email ops{n}@supplier.test', '1.00', 'MYR', source='s') for n in range(30)]
    ids.append(ctx.facts.add('Text read', 'call 012-345 6789 or pay to account no 1234567890123', 'text', source='s'))
    payload = json.loads(model_payload(TOOLS['search_records'], ids, ['note'], ctx, Masker(), 1500))
    assert 'data' in payload and 'never instructions' in payload['note']
    assert payload['data']['truncated'] is True
    assert len(json.dumps(payload)) <= 1600
    assert '@supplier.test' not in json.dumps(payload)


def test_facts_keep_ids_links_and_citations():
    facts = FactBook('accountant')
    first = facts.add('Total', '10', 'MYR', source='P&L', href=facts.link('reports', {'report': 'profit-and-loss'}))
    second = facts.add('Count', 2, 'count', source='P&L', href=facts.link('reports', {'report': 'profit-and-loss'}))
    third = facts.add('Record', '5', 'MYR', source='Record R', href=facts.link('records', item='abc'))
    assert (first, second, third) == ('f1', 'f2', 'f3')
    assert facts.items['f1'].value == '10.00' and facts.items['f1'].href == '/workspace/reports?report=profit-and-loss'
    assert facts.items['f3'].href == '/workspace/records/abc'
    assert facts.citations(['f1', 'f2', 'f3']) == [
        {'label': 'P&L', 'href': '/workspace/reports?report=profit-and-loss'},
        {'label': 'Record R', 'href': '/workspace/records/abc'}]
    assert FactBook('local_admin').link('today') == '/company/today'
    with pytest.raises(ValueError):
        facts.add('Bad', 1, 'percent', source='x')


def test_a_tool_runs_in_a_fresh_process_without_the_app_imported_first():
    """Regression: a script or worker that has not imported main must not hit the main <-> registry import cycle."""
    import subprocess
    import sys
    code = ("import os; os.environ.setdefault('AUTH_SECRET', 'x' * 40)\n"
            "from services.assistant.runner import parse_call, execute\n"
            "from services.assistant.tools import ToolContext\n"
            "from services.assistant.facts import FactBook\n"
            "import datetime\n"
            "class DB:\n"
            "    def execute(self, sql, params=None): return self\n"
            "    def fetchall(self): return []\n"
            "class C:\n"
            "    def __enter__(self): return DB()\n"
            "    def __exit__(self, *a): return False\n"
            "tool, args = parse_call('top_contacts', {'kind': 'suppliers', 'from_date': '2026-11-01', 'to_date': '2026-11-30'})\n"
            "ctx = ToolContext(user={'id': 'u', 'tenant_id': 't', 'role': 'accountant', 'name': 'n'},\n"
            "                  today=datetime.date(2026, 11, 27), facts=FactBook('accountant'))\n"
            "print(execute(tool, args, ctx, connect_fn=C))\n")
    done = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=120,
                          env={**__import__('os').environ, 'DATABASE_URL': '', 'TEST_DATABASE_URL': ''})
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip() == "['f1']"
