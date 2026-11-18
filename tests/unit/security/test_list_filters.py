"""Per-document filters on the exception and audit lists stay tenant-scoped and parameterised."""
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

import main  # noqa: F401  (routes import from main; load it first as the app does)
from services.routes.validation import routes as validation_routes
from services.routes.workspace import routes as workspace_routes

USER = {'id': 'user-1', 'tenant_id': 'tenant-a', 'role': 'accountant'}
DOC = '022ddbdd-47a5-4f3d-9740-cc07eba24c4d'


class Query(dict):
    def getlist(self, key):
        value = self.get(key, [])
        return value if isinstance(value, list) else [value]


def request(**query):
    req = MagicMock()
    req.query_params = Query(query)
    return req


def database():
    db = MagicMock()
    db.__enter__.return_value = db
    db.execute.return_value.fetchall.return_value = []
    return db


class ExceptionListFilterTests(unittest.TestCase):
    def run_list(self, **query):
        db = database()
        with patch.object(validation_routes, 'connect', return_value=db), \
             patch.object(validation_routes, 'require_workspace_user', return_value=USER), \
             patch.object(validation_routes, 'tenant_scope', return_value='tenant-a'):
            validation_routes.list_exceptions(request(**query))
        return db.execute.call_args.args

    def test_without_filter_the_query_is_unchanged(self):
        sql, params = self.run_list()
        self.assertNotIn('document_id=%s', sql)
        self.assertEqual(params, ['tenant-a', 'tenant-a', 'user-1', 'user-1'])

    def test_document_filter_is_a_parameter_inside_tenant_and_owner_scope(self):
        sql, params = self.run_list(documentId=DOC)
        self.assertIn('tenant_id=%s', sql); self.assertIn('owner_id=%s', sql)
        self.assertIn(' AND document_id=%s', sql)
        self.assertEqual(params[-1], DOC)
        self.assertNotIn(DOC, sql)

    def test_invalid_document_id_is_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            self.run_list(documentId="x' OR '1'='1")
        self.assertEqual(caught.exception.status_code, 422)


class AuditListFilterTests(unittest.TestCase):
    def run_list(self, **query):
        db = database()
        with patch.object(workspace_routes, 'connect', return_value=db), \
             patch.object(workspace_routes, 'require_workspace_user', return_value=USER), \
             patch.object(workspace_routes, 'tenant_scope', return_value='tenant-a'):
            workspace_routes.list_audit(request(**query))
        return db.execute.call_args.args

    def test_without_filter_the_newest_tenant_entries_are_returned(self):
        sql, params = self.run_list()
        self.assertNotIn('ANY', sql)
        self.assertEqual(params, ('tenant-a', 'tenant-a'))

    def test_resource_filter_is_parameterised_capped_and_tenant_scoped(self):
        names = [DOC, 'receipt.jpg', 'r1', 'r2', 'r3', 'r4', '']
        sql, params = self.run_list(resource=names)
        self.assertIn('tenant_id=%s', sql); self.assertIn('resource = ANY(%s)', sql)
        self.assertEqual(params[:2], ('tenant-a', 'tenant-a'))
        self.assertEqual(params[2], names[:5])
        self.assertNotIn(DOC, sql)


if __name__ == '__main__':
    unittest.main()
