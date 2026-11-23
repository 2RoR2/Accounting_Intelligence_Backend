from .accounting.routes import register as register_accounting
from .admin.routes import register as register_admin
from .bank.routes import register as register_bank
from .documents.routes import register as register_documents
from .erp.routes import register as register_erp
from .queue.routes import register as register_queue
from .reporting.routes import register as register_reporting
from .reporting.financial import register as register_financial_reporting
from .reporting.business_summary import register as register_business_summary
from .reporting.statement import register as register_statement
from .reporting.evaluation import register as register_evaluation
from .review.routes import register as register_review
from .security.routes import register as register_security
from .validation.routes import register as register_validation
from .workspace.routes import register as register_workspace
from .demo.routes import register as register_demo
from .effort.routes import register as register_effort
from .imports.routes import register as register_imports
from .home.routes import register as register_home

def register_component_routes(router, handlers):
    register_accounting(router, handlers)
    register_admin(router, handlers)
    register_bank(router, handlers)
    register_documents(router, handlers)
    register_erp(router, handlers)
    register_queue(router, handlers)
    register_reporting(router, handlers)
    register_financial_reporting(router)
    register_business_summary(router)
    register_statement(router)
    register_evaluation(router)
    register_review(router, handlers)
    register_security(router, handlers)
    register_validation(router, handlers)
    register_workspace(router, handlers)
    register_demo(router, handlers)
    register_effort(router, handlers)
    register_imports(router, handlers)
    register_home(router, handlers)
