"""ERP API route registrations."""

from . import attachments, catalog, contacts, documents, sharing


def register(router, handlers):
    products = "/api/erp/products"
    product = products + "/{product_id}"
    router.add_api_route(products, catalog.list_products, methods=["GET"], tags=["ERP Products"])
    router.add_api_route(products, catalog.create_product, methods=["POST"], status_code=201, tags=["ERP Products"])
    router.add_api_route(product, catalog.get_product, methods=["GET"], tags=["ERP Products"])
    router.add_api_route(product, catalog.update_product, methods=["PUT"], tags=["ERP Products"])
    router.add_api_route(product, catalog.delete_product, methods=["DELETE"], tags=["ERP Products"])

    catalog_root = "/api/erp/catalog"
    kind = catalog_root + "/{kind}"
    entry = kind + "/{entry_id}"
    router.add_api_route(catalog_root, catalog.catalog_kinds, methods=["GET"], tags=["ERP Catalog"])
    router.add_api_route(kind, catalog.list_catalog, methods=["GET"], tags=["ERP Catalog"])
    router.add_api_route(kind, catalog.create_catalog, methods=["POST"], status_code=201, tags=["ERP Catalog"])
    router.add_api_route(entry, catalog.get_catalog, methods=["GET"], tags=["ERP Catalog"])
    router.add_api_route(entry, catalog.update_catalog, methods=["PUT"], tags=["ERP Catalog"])
    router.add_api_route(entry, catalog.delete_catalog, methods=["DELETE"], tags=["ERP Catalog"])

    contacts.register(router)
    documents.register(router)
    sharing.register(router)
    attachments.register(router)
