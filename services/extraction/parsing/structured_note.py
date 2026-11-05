"""Read UBL JSON note values by exact pointers, retaining the original payload."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from decimal import Decimal
from services.extraction.contracts.accounting_contract import map_m2_result


def extract_structured_note(document_id, path, root):
    source=Path(path).resolve()
    if not source.is_relative_to(Path(root).resolve()):
        raise ValueError('STRUCTURED_SOURCE_OUTSIDE_TRUSTED_ROOT')
    data=source.read_bytes()
    if len(data)>20*1024*1024:
        raise ValueError('STRUCTURED_SOURCE_SIZE_LIMIT')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result: raise ValueError('DUPLICATE_STRUCTURED_KEY')
            result[key]=value
        return result
    raw=json.loads(data,object_pairs_hook=unique,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('INVALID_JSON_NUMBER')))
    if not isinstance(raw,dict) or not isinstance(raw.get('Invoice'),list) or len(raw['Invoice'])!=1:
        raise ValueError('EXPECTED_ONE_UBL_DOCUMENT')
    invoice=json.loads(data,object_pairs_hook=unique,parse_float=Decimal)['Invoice'][0]
    def read(parts):
        node=invoice; pointer='#/Invoice/0'
        for index,name in enumerate(parts):
            if not isinstance(node,dict) or name not in node: return None,pointer
            node=node[name];pointer+='/'+name
            if isinstance(node,list):
                choices=list(enumerate(node))
                if len(choices)!=1 and index+1<len(parts):
                    choices=[(i,item) for i,item in choices if isinstance(item,dict) and parts[index+1] in item]
                if len(choices)!=1:return None,pointer
                chosen,node=choices[0];pointer+='/'+str(chosen)
        if isinstance(node,dict):node=node.get('_');pointer+='/_'
        return (str(node) if isinstance(node,(str,int,Decimal)) and not isinstance(node,bool) else None),pointer
    code,_=read(['InvoiceTypeCode'])
    kind={'02':'credit_note','03':'debit_note'}.get(code)
    if not kind:raise ValueError('UNSUPPORTED_STRUCTURED_DOCUMENT_TYPE')
    def cell(parts):
        value,pointer=read(parts)
        placeholder=value and (value.casefold() in {"supplier's name","buyer's name","supplier's tin","supplier's brn","na","n/a"})
        evidence=[{'source_ref':pointer,'text':value,'source_format':'ubl-json'}] if value else []
        return {'state':'ambiguous' if placeholder else 'resolved' if value else 'missing',
                'value':None if placeholder else value,'raw_text':value,'method':'parser_mapping',
                'confidence':None,'evidence':evidence,
                'candidates':[{'source':'parser','value':value,'raw_text':value,'auto_accepted':False,
                               'evidence':evidence,'placeholder':True}] if placeholder else []}
    paths={
        'reference':['ID'],'date':['IssueDate'],'currency':['DocumentCurrencyCode'],
        'supplier':['AccountingSupplierParty','Party','PartyLegalEntity','RegistrationName'],
        'customer':['AccountingCustomerParty','Party','PartyLegalEntity','RegistrationName'],
        'invoice':['BillingReference','InvoiceDocumentReference','ID'],
        'total':['LegalMonetaryTotal','TaxInclusiveAmount'],
        'subtotal':['LegalMonetaryTotal','LineExtensionAmount'],
        'amount_due':['LegalMonetaryTotal','PayableAmount'],
        'tax':['TaxTotal','TaxAmount'],
    }
    run='structured-note-'+uuid4().hex
    result={'schema_version':'0.1.0-proposed','mode':'module2_public_result','document_id':document_id,
            'document_type':{'value':kind,'status':'classified','reason':'STRUCTURED_SOURCE_TYPE_CODE',
                             'evidence':[{'source_ref':'#/Invoice/0/InvoiceTypeCode/0/_','text':code}]},
            'status':'human_review_required','review_required':True,'accounting_approval_performed':False,
            'safety':{'ai_candidate_auto_accepted':False,'automatic_accounting_approval':False},
            'fields':{name:cell(parts) for name,parts in paths.items()},'line_items':[],
            'routing':{'decision':'human_review','blocking_reasons':[{'code':'NOTE_POLICY_REVIEW_REQUIRED'}]},
            'provenance':{'service_run_id':run,'source_sha256':hashlib.sha256(data).hexdigest(),
                          'source_format':'ubl-json','extractor_version':'structured-note/1'},'payload':raw}
    # Keep strict decimal/date/field validation at the existing persistence boundary.
    map_m2_result(result,document_id)
    target=Path(root)/'runs'/run
    target.mkdir(parents=True,exist_ok=False)
    artifact=target/'result.json'
    artifact.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'execution_status':'EXTRACTED','document_id':document_id,'extraction_status':'human_review_required',
            'service_run_id':run,'public_result_path':str(artifact),'review_required':True,
            'accounting_approval_performed':False,'error':None}
