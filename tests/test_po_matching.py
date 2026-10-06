from validation.po_matching import match_invoice_to_po


def test_po_match_success():
    invoice = {
        "vendor_name": "ABC Supplies",
        "line_items": [
            {
                "description": "Office Paper",
                "quantity": 10,
                "unit_price": "5.00",
                "amount": "50.00",
            }
        ],
        "total_amount": "50.00",
    }

    purchase_order = {
        "po_number": "PO-001",
        "vendor_name": "ABC Supplies",
        "line_items": [
            {
                "description": "Office Paper",
                "quantity": 10,
                "unit_price": "5.00",
            }
        ],
        "total_amount": "50.00",
    }

    result = match_invoice_to_po(invoice, purchase_order)

    assert result["status"] == "MATCHED"
    assert result["supplier_match"] is True
    assert result["po_amount_variance"] == 0.0
    assert result["variances"] == []


def test_po_match_supplier_mismatch():
    invoice = {
        "vendor_name": "ABC Supplies",
        "line_items": [
            {
                "description": "Office Paper",
                "quantity": 10,
                "unit_price": "5.00",
                "amount": "50.00",
            }
        ],
        "total_amount": "50.00",
    }

    purchase_order = {
        "po_number": "PO-002",
        "vendor_name": "XYZ Supplies",
        "line_items": [
            {
                "description": "Office Paper",
                "quantity": 10,
                "unit_price": "5.00",
            }
        ],
        "total_amount": "50.00",
    }

    result = match_invoice_to_po(invoice, purchase_order)

    assert result["status"] == "REVIEW_REQUIRED"
    assert result["supplier_match"] is False
    assert any(
        variance["type"] == "SUPPLIER_MISMATCH"
        for variance in result["variances"]
    )


def test_po_match_quantity_variance():
    invoice = {
        "vendor_name": "ABC Supplies",
        "line_items": [
            {
                "description": "Office Paper",
                "quantity": 12,
                "unit_price": "5.00",
                "amount": "60.00",
            }
        ],
        "total_amount": "60.00",
    }

    purchase_order = {
        "po_number": "PO-003",
        "vendor_name": "ABC Supplies",
        "line_items": [
            {
                "description": "Office Paper",
                "quantity": 10,
                "unit_price": "5.00",
            }
        ],
        "total_amount": "50.00",
    }

    result = match_invoice_to_po(invoice, purchase_order)

    assert result["status"] == "REVIEW_REQUIRED"
    assert result["po_amount_variance"] == 10.0
    assert any(
        variance["type"] == "QUANTITY_VARIANCE"
        for variance in result["variances"]
    )

def test_po_match_unit_price_variance():
    invoice = {
        "vendor_name": "ABC Supplies",
        "line_items": [
            {
                "description": "Office Paper",
                "quantity": 10,
                "unit_price": "6.00",
                "amount": "60.00",
            }
        ],
        "total_amount": "60.00",
    }

    purchase_order = {
        "po_number": "PO-004",
        "vendor_name": "ABC Supplies",
        "line_items": [
            {
                "description": "Office Paper",
                "quantity": 10,
                "unit_price": "5.00",
            }
        ],
        "total_amount": "50.00",
    }

    result = match_invoice_to_po(invoice, purchase_order)

    assert result["status"] == "REVIEW_REQUIRED"
    assert any(
        variance["type"] == "UNIT_PRICE_VARIANCE"
        for variance in result["variances"]
    )
