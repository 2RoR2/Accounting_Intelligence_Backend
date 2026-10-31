"""Sanitized layout regressions; no private documents or expected customer data."""
import unittest
from tests.extraction.test_financial_layout import layout
from tests.extraction.test_multidocument import contract


class MalaysianLayoutTests(unittest.TestCase):
    def test_compound_customer_account_label_is_not_a_customer_name(self):
        raw, mapped = contract(layout('TAX INVOICE', [
            ('Customer Account', 20, 80, 150, 100), ('AC-432', 20, 105, 140, 125),
            ('Billing Address', 20, 150, 160, 170),
            ('Example Technology Hub', 20, 175, 250, 195),
            ('17 Example Street', 20, 200, 250, 220),
            ('Delivery Address', 330, 150, 490, 170)]))
        self.assertEqual(raw['fields']['buyer_name']['value'], 'Example Technology Hub')
        self.assertNotEqual(next(f for f in mapped['fields'] if f['field_key']=='customer')['current_value'], 'Account')

    def test_account_header_alone_does_not_fill_customer(self):
        raw, _ = contract(layout('INVOICE', [('Customer Account',20,80,150,100),('ACC-001',20,105,150,125)]))
        self.assertTrue('buyer_name' not in raw['fields'] or raw['fields']['buyer_name']['value'] is None)
        raw,_=contract(layout('INVOICE',[('Customer Account AC-432',20,80,300,100)]))
        self.assertTrue('buyer_name' not in raw['fields'] or raw['fields']['buyer_name']['value'] is None)

    def test_doc_number_and_right_aligned_footer_values(self):
        raw, _ = contract(layout('INVOICE', [
            ('Doc No.', 350, 60, 430, 80), ('X-204', 350, 85, 460, 105),
            ('Date',500,60,590,80),('24/02/2025',500,85,590,105),
            ('Tax Amount (MYR)', 300, 500, 440, 520), ('0.00', 400, 525, 440, 545),
            ('Total Payable (MYR)', 450, 500, 590, 520), ('72.40', 540, 525, 590, 545)]))
        self.assertEqual(raw['fields']['invoice_number']['value'], 'X-204')
        self.assertEqual(raw['fields']['tax_amount']['value'], '0.00')
        self.assertEqual(raw['fields']['total_amount']['value'], '72.40')

    def test_receipt_total_sales_conflicts_with_literal_zero_total(self):
        raw, mapped = contract(layout('RECEIPT', [
            ('Total: 0.00',20,400,220,420), ('Total Sales',200,440,340,460),('72.40',480,440,570,460),
            ('CASH',220,480,340,500),('72.40',480,480,570,500)]))
        total=raw['fields']['total_amount']
        self.assertIsNone(total['value'])
        self.assertEqual({c['value'] for c in total['candidates']},{'0.00','72.40'})
        self.assertTrue(total['evidence'])
        from services.extraction.contracts.reliability import field_guidance
        guidance=field_guidance(next(f for f in mapped['fields'] if f['field_key']=='total'))
        self.assertTrue(any(w['code']=='CONFLICTING_TOTALS' for w in guidance['warnings']))
        self.assertEqual(raw['fields']['payment_method']['value'],'cash')
        self.assertIsNone(raw['fields']['currency']['value'])

    def test_enterprise_heading_recovers_merchant_not_person_name(self):
        raw,_=contract(layout('RECEIPT', [('Example Person',20,45,200,65),
            ('EXAMPLE MACHINERY ENTERPRISE',20,80,450,100),('Time: 08:35:11',20,130,260,150)]))
        self.assertEqual(raw['fields']['merchant']['value'],'EXAMPLE MACHINERY ENTERPRISE')
        self.assertEqual(raw['fields']['transaction_time']['value'],'08:35:11')
        self.assertEqual(raw['fields']['transaction_time']['method'],'OCR')

    def test_invoice_multicolumn_item_and_aggregate_summary(self):
        doc=layout('TAX INVOICE', [('Currency: MYR',10,40,180,60),
            ('No',10,100,30,115),('Description',40,100,150,115),('Qty',220,100,250,115),
            ('Price/Unit',300,100,370,115),('Discount',380,100,430,115),('Sub Total',440,100,490,115),
            ('Tax Amt',500,100,540,115),('Total Incl. Tax',550,100,595,115),
            ('1',10,125,30,145),('Synthetic chair',40,125,190,145),('2.00',220,125,250,145),
            ('30.00',315,125,370,145),('60.00',450,125,490,145),('0.00',510,125,540,145),('60.00',550,125,595,145),
            ('(Blue)',40,147,90,165),
            ('Total Amount Due',100,600,250,620),('60.00',450,600,490,620),('0.00',510,600,540,620),('60.00',550,600,595,620)])
        raw,mapped=contract(doc)
        self.assertEqual(raw['fields']['subtotal']['value'],'60.00')
        self.assertEqual(len(mapped['line_items']),1)
        row=mapped['line_items'][0]
        self.assertEqual((row['description'],row['quantity'],row['unit_price'],row['amount']),('Synthetic chair (Blue)','2.00','30.00','60.00'))
        self.assertIsNone(raw['fields'].get('due_date',{}).get('value'))

    def test_line_tax_and_subtotal_columns_are_not_document_totals_without_summary(self):
        raw,_=contract(layout('INVOICE', [('Description',20,100,180,120),('Qty',240,100,280,120),
            ('Sub Total',350,100,420,120),('Tax Amt',440,100,500,120),('Total Incl. Tax',510,100,595,120),
            ('Part A',20,140,180,160),('1',240,140,280,160),('50.00',350,140,420,160),('3.00',440,140,500,160),('53.00',540,140,595,160)]))
        self.assertIsNone(raw['fields']['subtotal']['value'])
        self.assertIsNone(raw['fields']['tax_amount']['value'])

    def test_unknown_money_and_date_do_not_gain_defaults(self):
        raw,_=contract(layout('RECEIPT',[('Date: 04/05/2025',20,70,270,90),('Time: 08:35:11',20,100,270,120)]))
        for key in ('total_amount','currency','transaction_date'):self.assertIsNone(raw['fields'][key]['value'])
        self.assertEqual(raw['fields']['transaction_time']['value'],'08:35:11')

    def test_split_labels_keep_their_own_values_despite_slight_ocr_overlap(self):
        raw,_=contract(layout('RECEIPT',[
            ('Doc No.',10,70,70,90),('R-642',110,70,180,90),
            ('Date:',260,70,310,90),('24/02/2025',308,70,400,90),
            ('Time:',260,100,314,120),('08:35:11',306,100,390,120)]))
        self.assertEqual(raw['fields']['receipt_number']['value'],'R-642')
        self.assertEqual(raw['fields']['transaction_date']['value'],'2025-02-24')
        self.assertEqual(raw['fields']['transaction_time']['value'],'08:35:11')

    def test_payment_method_below_label_does_not_take_next_tax_heading(self):
        raw,_=contract(layout('RECEIPT',[('Payment Method',20,100,210,130),
            ('VISA',30,150,90,175),('Tax inclusive:',20,170,200,195)]))
        self.assertEqual(raw['fields']['payment_method']['value'],'card')

    def test_sku_line_above_description_keeps_first_overlapping_row(self):
        _,mapped=contract(layout('RECEIPT',[
            ('Item',20,100,100,120),('Qty',200,100,240,120),
            ('S/Price',280,100,340,120),('S/Price',400,100,460,120),('Amount',500,100,570,120),
            ('SKU-101',20,121,120,141),('1',210,119,230,139),
            ('7.50',300,118,340,138),('7.50',420,118,460,138),('7.50',530,118,570,138),
            ('Synthetic item A',20,145,240,165),
            ('SKU-102',20,190,120,210),('2',210,190,230,210),
            ('8.00',300,190,340,210),('8.00',420,190,460,210),('16.00',530,190,570,210),
            ('Synthetic item B',20,215,240,235),('Total Qty',20,250,160,270),('3',210,250,230,270)]))
        self.assertEqual([(r['description'],r['quantity'],r['unit_price'],r['amount']) for r in mapped['line_items']],
                         [('Synthetic item A','1','7.50','7.50'),('Synthetic item B','2','8.00','16.00')])


if __name__=='__main__':unittest.main()
