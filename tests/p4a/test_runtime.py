"""Actual restricted-role and startup observations; no privilege workaround."""
import asyncio
import json
import unittest
import psycopg
from tests import p4a_support as h

class RuntimeTests(unittest.TestCase):
    def test_restricted_role_has_no_owner_or_ddl_privileges(self):
        with h.connect_runtime() as db:
            row=db.execute("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
            self.assertFalse(any(row.values()))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with db.transaction():
                    db.execute('CREATE TABLE public.p4a_forbidden_probe(id int)')

    def test_startup_succeeds_under_restricted_role(self):
        import app
        facts={}
        async def startup():
            async with app.lifespan(app.app):pass
        try:asyncio.run(startup())
        except Exception as exc:
            facts={'exception':type(exc).__name__,'sqlstate':getattr(exc,'sqlstate',None),
                   'operation':'actual FastAPI lifespan / compatibility ALTER'}
            (h.RUN/'evidence/phase4ar'/'startup-failure.json').write_text(json.dumps(facts,indent=2))
            raise
