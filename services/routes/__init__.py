"""HTTP route registry for the Accounting Intelligence backend.

The route handlers are being migrated out of ``main.py`` by component.  The
shared router keeps registration in one place while preserving the existing
``/api`` URLs and frontend contracts during that migration.
"""

from fastapi import APIRouter

router = APIRouter()

__all__ = ["router"]
