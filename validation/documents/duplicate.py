from typing import Any, Dict, List


def find_duplicate_invoices(invoices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Find invoices that share the same vendor and invoice number.

    Returns a list of duplicate groups.
    """

    seen = {}
    duplicates = []

    for invoice in invoices:
        vendor = invoice.get('vendor_name')
        number = invoice.get('invoice_number')
        vendor_name = vendor.strip().casefold() if isinstance(vendor, str) else ''
        invoice_number = number.strip() if isinstance(number, str) else ''

        if not vendor_name or not invoice_number:
            continue

        key = (vendor_name, invoice_number)

        if key in seen:
            duplicates.append({
                "code": "EX-007",
                "vendor_name": invoice.get("vendor_name"),
                "invoice_number": invoice.get("invoice_number"),
                "source_file": invoice.get("source_file"),
                "duplicate_of": seen[key],
            })
        else:
            seen[key] = invoice.get("source_file")

    return duplicates
