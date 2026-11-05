from .accounting.routes import register as register_accounting
from .admin.routes import register as register_admin
from .bank.routes import register as register_bank
from .documents.routes import register as register_documents
from .queue.routes import register as register_queue
from .reporting.routes import register as register_reporting
from .review.routes import register as register_review
from .security.routes import register as register_security
from .validation.routes import register as register_validation
from .workspace.routes import register as register_workspace
from .demo.routes import register as register_demo

def register_component_routes(router, handlers):
    register_accounting(router, handlers)
    register_admin(router, handlers)
    register_bank(router, handlers)
    register_documents(router, handlers)
    register_queue(router, handlers)
    register_reporting(router, handlers)
    register_review(router, handlers)
    register_security(router, handlers)
    register_validation(router, handlers)
    register_workspace(router, handlers)
    register_demo(router, handlers)
