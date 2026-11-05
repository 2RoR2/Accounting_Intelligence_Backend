"""Small reproducible, invented digital PDFs for live CPU Docling acceptance.

No notebook extraction JSON or customer data. Run as a module to write PDFs.
"""
from pathlib import Path


def digital_pdf(title, labels, headings=None, values=None):
    def text(x, y, value, size=12):
        value = value.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
        return f'BT /F1 {size} Tf {x} {y} Td ({value}) Tj ET\n'
    stream = text(40, 800, title, 17)
    for i, label in enumerate(labels):
        stream += text(40, 770 - i * 25, label, 11)
    if headings:
        # Visible ruled table, separate from the explicit header labels.
        xs = [40, 280, 360, 450, 555] if len(headings) == 4 else [40, 400, 555]
        stream += f'0.5 w 40 300 515 80 re S\n40 340 m 555 340 l S\n'
        for x in xs[1:-1]:
            stream += f'{x} 300 m {x} 380 l S\n'
        for x, heading, value in zip(xs, headings, values):
            stream += text(x + 8, 355, heading, 11) + text(x + 8, 315, value, 11)
    stream += text(40, 220, 'SYNTHETIC TEST DATA. Invented layout. Not a real transaction.', 10)
    data = stream.encode('ascii')
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               f'<< /Length {len(data)} >>\nstream\n'.encode() + data + b'endstream']
    pdf, offsets = b'%PDF-1.4\n', [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
    start = len(pdf)
    pdf += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    pdf += b''.join(f'{offset:010} 00000 n \n'.encode() for offset in offsets[1:])
    return pdf + f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode()


def fixtures():
    return {
        'invoice_b.pdf': digital_pdf('TAX INVOICE - SYNTHETIC TEST DATA', [
            'Vendor: Synthetic Services B', 'Customer: Synthetic Buyer', 'Invoice no: SYN-B-002',
            'Invoice date: 2026-10-19', 'Due date: 2026-11-19', 'Currency: MYR',
            'Subtotal: 50.00', 'Discount: 5.00', 'Tax: 3.00', 'Total: 48.00'],
            ['Description', 'Quantity', 'Unit price', 'Line total'], ['Service hours', '2.5', '20.00', '50.00']),
        'utility_bill.pdf': digital_pdf('UTILITY BILL - SYNTHETIC TEST DATA', [
            'Issuer: Synthetic Utilities', 'Account number: SYN-ACC-001', 'Bill reference: SYN-BILL-001',
            'Billing period: 2026-09-01 to 2026-09-30', 'Bill date: 2026-10-01',
            'Due date: 2026-10-31', 'Currency: MYR', 'Amount due: 80.00'],
            ['Description', 'Line total'], ['Service charge', '80.00']),
        'retail_receipt.pdf': digital_pdf('RETAIL RECEIPT - SYNTHETIC TEST DATA', [
            'Merchant: Synthetic Retail', 'Transaction date: 2026-10-19', 'Transaction time: 14:35:20',
            'Receipt number: SYN-R-001', 'Currency: MYR', 'Subtotal: 10.00', 'Tax: 0.60',
            'Total: 10.60', 'Payment method: Card'],
            ['Description', 'Quantity', 'Unit price', 'Line total'], ['Pencil pack', '2', '5.00', '10.00']),
        'unknown.pdf': digital_pdf('MEETING AGENDA - SYNTHETIC TEST DATA', [
            'Subject: Synthetic planning meeting', 'Date: 2026-10-19',
            'Total: 999.00', 'Discuss document handling. No accounting transaction.']),
    }


if __name__ == '__main__':
    target = Path(__file__).parent / 'data/multidocument'
    target.mkdir(parents=True, exist_ok=True)
    for name, pdf in fixtures().items():
        (target / name).write_bytes(pdf)
        print(name, len(pdf), 'bytes (synthetic)')
