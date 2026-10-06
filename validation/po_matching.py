from decimal import Decimal
from typing import Any, Dict, List

from validation.models import AMOUNT_TOLERANCE


def match_invoice_to_po(
    invoice_data: Dict[str, Any],
    purchase_order: Dict[str, Any],
) -> dict:
    """
    Match a validated invoice against a purchase order.

    This is a standalone PO matching component. It does not access
    the database because PO tables are not implemented yet.

    Returns MATCHED when the supplier, line items, quantities,
    unit prices, and total amount are consistent with the PO.
    Otherwise, returns REVIEW_REQUIRED with the detected variances.
    """

    variances: List[dict] = []

    invoice_vendor = str(invoice_data.get("vendor_name", "")).strip().lower()
    po_vendor = str(purchase_order.get("vendor_name", "")).strip().lower()

    supplier_match = invoice_vendor == po_vendor

    if not supplier_match:
        variances.append({
            "type": "SUPPLIER_MISMATCH",
            "message": (
                f"Invoice vendor '{invoice_data.get('vendor_name')}' "
                f"does not match PO vendor "
                f"'{purchase_order.get('vendor_name')}'."
            ),
        })

    invoice_lines = invoice_data.get("line_items", [])
    po_lines = purchase_order.get("line_items", [])

    if len(invoice_lines) != len(po_lines):
        variances.append({
            "type": "LINE_COUNT_MISMATCH",
            "message": (
                f"Invoice contains {len(invoice_lines)} line item(s), "
                f"but PO contains {len(po_lines)} line item(s)."
            ),
        })

    line_results = []

    for index, invoice_line in enumerate(invoice_lines):
        if index >= len(po_lines):
            break

        po_line = po_lines[index]

        invoice_description = str(
            invoice_line.get("description", "")
        ).strip().lower()
        po_description = str(
            po_line.get("description", "")
        ).strip().lower()

        description_match = invoice_description == po_description

        invoice_quantity = Decimal(str(invoice_line.get("quantity", 0)))
        po_quantity = Decimal(str(po_line.get("quantity", 0)))

        quantity_variance = invoice_quantity - po_quantity

        invoice_unit_price = Decimal(
            str(invoice_line.get("unit_price", 0))
        )
        po_unit_price = Decimal(
            str(po_line.get("unit_price", 0))
        )

        unit_price_variance = invoice_unit_price - po_unit_price

        if not description_match:
            variances.append({
                "type": "ITEM_MISMATCH",
                "line": index + 1,
                "message": (
                    f"Invoice item '{invoice_line.get('description')}' "
                    f"does not match PO item "
                    f"'{po_line.get('description')}'."
                ),
            })

        if quantity_variance != 0:
            variances.append({
                "type": "QUANTITY_VARIANCE",
                "line": index + 1,
                "variance": float(quantity_variance),
            })

        if abs(unit_price_variance) > AMOUNT_TOLERANCE:
            variances.append({
                "type": "UNIT_PRICE_VARIANCE",
                "line": index + 1,
                "variance": float(unit_price_variance),
            })

        line_results.append({
            "line": index + 1,
            "description_match": description_match,
            "quantity_variance": float(quantity_variance),
            "unit_price_variance": float(unit_price_variance),
        })

    invoice_total = Decimal(str(invoice_data.get("total_amount", 0)))
    po_total = Decimal(str(purchase_order.get("total_amount", 0)))

    po_amount_variance = invoice_total - po_total

    if abs(po_amount_variance) > AMOUNT_TOLERANCE:
        variances.append({
            "type": "PO_AMOUNT_VARIANCE",
            "variance": float(po_amount_variance),
        })

    status = "MATCHED" if not variances else "REVIEW_REQUIRED"

    return {
        "status": status,
        "po_number": purchase_order.get("po_number"),
        "supplier_match": supplier_match,
        "line_results": line_results,
        "po_amount_variance": float(po_amount_variance),
        "variances": variances,
    }