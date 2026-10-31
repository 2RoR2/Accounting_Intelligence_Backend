"""Public identity must match document UUIDs without changing session authority."""
import unittest
from uuid import UUID
from unittest.mock import patch
import main


class PublicIdentityTests(unittest.TestCase):
    def test_document_detail_retains_nullable_assignment_for_review_permissions(self):
        tenant='11111111-1111-4111-8111-111111111111'
        owner='22222222-2222-4222-8222-222222222222'
        assignee='33333333-3333-4333-8333-333333333333'
        row={'id':'44444444-4444-4444-8444-444444444444','tenant_id':tenant,'owner_id':owner,
             'document_code':'SYNTHETIC','original_filename':'synthetic.pdf','kind':'invoice',
             'category':'accounts_payable','status':'extracted','mime_type':'application/pdf',
             'file_size_bytes':1,'content_revision':2,'uploaded_at':'2026-10-27','updated_at':'2026-10-27'}
        class Database:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def execute(self,sql,params):
                self.sql=sql;self.params=params;return self
            def fetchone(self):return self.row
        for api in (app,main):
            for assigned in (None,assignee):
                with self.subTest(entrypoint=api.__name__,assigned=assigned is not None):
                    db=Database();db.row={**row,'assigned_to':assigned}
                    with patch.object(api,'connect',return_value=db),patch.object(api,'require_workspace_user',return_value={'id':assignee,'role':'accountant','tenant_id':tenant}):
                        result=api.get_document_detail(row['id'],None)
                    self.assertEqual(result['assignedTo'],assigned)
                    self.assertEqual(result['ownerId'],owner)
                    self.assertEqual(result['tenantId'],tenant)
                    self.assertIn('assigned_to=%s',db.sql)
                    self.assertEqual(db.params,[row['id'],tenant,tenant,assignee,assignee])

    def test_authenticated_identity_separates_tenant_uuid_from_display_code(self):
        tenant=UUID('11111111-1111-4111-8111-111111111111')
        user={'id':UUID('22222222-2222-4222-8222-222222222222'),'tenant_id':tenant,
              'tenant_code':'TENANT_123456','name':'Synthetic accountant','email':'synthetic@example.test',
              'role':'accountant','status':'active','must_change_password':False,
              'invited_by':None,'invited_at':None,'last_login_at':None}
        for api in (app,main):
            with self.subTest(entrypoint=api.__name__):
                result=api.public_user(user)
                self.assertEqual(result['tenantId'],str(tenant))
                self.assertEqual(result['tenantCode'],'TENANT_123456')
                self.assertNotEqual(result['tenantId'],result['tenantCode'])
                self.assertEqual(result['id'],str(user['id']))
                platform=api.public_user({**user,'role':'super_admin','tenant_id':None,'tenant_code':None})
                self.assertIsNone(platform['tenantId']);self.assertIsNone(platform['tenantCode'])
