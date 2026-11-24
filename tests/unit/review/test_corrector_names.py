"""'Corrected by Jonathan, 25 Nov': the extraction route names who corrected a field, within the company only."""
import main  # noqa: F401 - initialise the route registry before importing route modules.
from services.routes.documents.routes import _user_names


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class Db:
    def __init__(self):
        self.queries = []

    def execute(self, sql, params):
        self.queries.append((sql, params))
        tenant, wanted = params
        people = {('t1', 'u1'): 'Jonathan', ('t2', 'u2'): 'Someone Else'}
        return Rows([{'id': uid, 'name': name} for (t, uid), name in people.items() if t == tenant and uid in wanted])


def test_names_come_from_this_company_only():
    db = Db()
    assert _user_names(db, 't1', ['u1', 'u2', None, 'u1']) == {'u1': 'Jonathan'}
    sql, params = db.queries[0]
    assert 'tenant_id=%s' in sql and params == ('t1', ['u1', 'u2'])


def test_no_corrections_reads_nothing():
    db = Db()
    assert _user_names(db, 't1', [None, None]) == {}
    assert db.queries == []
