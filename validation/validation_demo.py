from pydantic import ValidationError
from validation.models import Invoice


VALID_INVOICE = {
    "invoice_number": "INV-2026-0091",
    "vendor_name": "Sinar Trading Sdn Bhd",
    "invoice_date": "2026-09-01",
    "due_date": "2026-09-30",
    "line_items": [
        {
            "description": "A4 Paper (Ream)",
            "quantity": 10,
            "unit_price": "12.50",
            "amount": "125.00",
        },
        {
            "description": "Printer Toner",
            "quantity": 2,
            "unit_price": "180.00",
            "amount": "360.00",
        },
    ],
    "subtotal": "485.00",
    "tax_amount": "29.10",
    "total_amount": "514.10",
    "currency": "MYR",
}


INVALID_INVOICE = {
    **VALID_INVOICE,
    "total_amount": "999.99",
}


def validate_invoice(name, invoice_data):
    print(f"\n{name}")
    print("-" * 40)

    try:
        Invoice(**invoice_data)
        print("VALID")
        print("  Invoice passed accounting validation.")

    except ValidationError as error:
        print("INVALID")
        print("  Invoice failed accounting validation:")

        for item in error.errors():
            print(f"  - {item['msg']}")


validate_invoice("VALID INVOICE", VALID_INVOICE)
validate_invoice("INVALID INVOICE", INVALID_INVOICE)
