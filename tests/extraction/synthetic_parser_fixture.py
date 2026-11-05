"""Invented Docling-shaped parser output for injected-parser tests only.

This replaces a missing external run artifact. It is not genuine OCR evidence.
Values come from the already-committed synthetic M2 contract fixture.
"""
import json
from pathlib import Path


def document():
    result=json.loads((Path(__file__).parent/'data/contracts/syn001_m2.json').read_text())
    labels={'supplier_name':'Supplier','invoice_number':'Invoice number','invoice_date':'Date',
            'currency':'Currency','subtotal':'Subtotal','tax_amount':'Tax amount','total_amount':'Total amount'}
    texts=['INVOICE']+[label+': '+result['fields'][key]['value'] for key,label in labels.items()]
    doc={'schema_name':'DoclingDocument','version':'synthetic-test-only','name':'SYN-001',
         'pages':{'1':{'size':{'width':595,'height':842}}},'texts':[], 'tables':[]}
    for i,text in enumerate(texts):
        doc['texts'].append({'text':text,'self_ref':f'#/texts/{i}','label':'text',
            'prov':[{'page_no':1,'bbox':{'l':20,'t':20+i*30,'r':500,'b':40+i*30,'coord_origin':'TOPLEFT'}}]})
    keys=['description','quantity','unit_price','line_total']
    cells=[]
    values=[['Description','Quantity','Unit price','Line total']]+[[row[key]['value'] for key in keys] for row in result['line_items']]
    for r,row in enumerate(values):
        for c,text in enumerate(row):
            cells.append({'text':text,'start_row_offset_idx':r,'start_col_offset_idx':c,'row_span':1,'col_span':1,
                'column_header':r==0,'bbox':{'l':20+c*130,'t':350+r*30,'r':140+c*130,'b':370+r*30,'coord_origin':'TOPLEFT'}})
    doc['tables']=[{'self_ref':'#/tables/0','prov':[{'page_no':1}],
        'data':{'num_rows':len(values),'num_cols':4,'table_cells':cells}}]
    return doc
