"""Provider and selective execution tests use a fake server, never real AI."""
import hashlib
import json
import os
from io import BytesIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, MagicMock, patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from services.extraction.classification.classify_document import classify_document
from services.extraction.ai import qwen_provider as qp
from services.extraction.ai.qwen_provider import prepare_request, validate_response, OllamaQwenProvider, parse_json
from services.extraction.ai.run_selective_qwen import run_attempt, render_single_page
from .test_route_extraction import fixture

class ProviderTests(unittest.TestCase):
    def response(self, content):
        return {'done': True, 'message': {'content': content}}

    def test_only_requested_field_in_schema(self):
        p = prepare_request(b'image', ['invoice_number'])
        self.assertEqual(set(p['format']['properties']), {'invoice_number'})
        self.assertEqual(p['options']['num_ctx'], 4096)
        self.assertFalse(p['stream'])

    def test_null_is_valid(self):
        self.assertEqual(validate_response(self.response('{"invoice_number":null}'), ['invoice_number']),
                         {'invoice_number': None})

    def test_extra_fields_rejected(self):
        with self.assertRaises(ValueError):
            validate_response(self.response('{"invoice_number":null,"total_amount":"1"}'), ['invoice_number'])

    def test_missing_fields_rejected(self):
        with self.assertRaises(ValueError): validate_response(self.response('{}'), ['invoice_number'])

    def test_duplicate_fields_rejected(self):
        with self.assertRaises(ValueError):
            validate_response(self.response('{"invoice_number":"A","invoice_number":"B"}'), ['invoice_number'])

    def test_bad_values_rejected(self):
        for value in (12, True, '', '   ', [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_response(self.response(json.dumps({'invoice_number': value})), ['invoice_number'])

    def test_incomplete_response_rejected(self):
        with self.assertRaises(ValueError): validate_response({'done': False}, ['invoice_number'])

    def test_unknown_and_duplicate_requests_rejected(self):
        for fields in ([], ['unsupported'], ['invoice_number', 'invoice_number']):
            with self.subTest(fields=fields), self.assertRaises(ValueError): prepare_request(b'image', fields)

    def test_structured_values_and_nulls_are_candidates_not_proof(self):
        values={'supplier_name':'Example Trading','invoice_number':'X-42','invoice_date':'2025-12-25',
                'currency':'MYR','subtotal':'10.125','tax_amount':None,'total_amount':'10.125'}
        response=self.response(json.dumps(values));response.update(model=qp.MODEL,done_reason='stop')
        self.assertEqual(validate_response(response,list(values),model=qp.MODEL),values)

    def test_unsupported_semantic_values_rejected_without_repair(self):
        for field,value in [('invoice_date','25/12/2025'),('invoice_date','2025-02-30'),
                            ('currency','RM'),('currency','$'),('total_amount','paid'),
                            ('total_amount','RM 10.00'),('tax_amount','NaN'),('subtotal','1,000.00')]:
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):
                validate_response(self.response(json.dumps({field:value})),[field])

    def test_malformed_envelopes_wrong_models_and_truncation_rejected(self):
        for mutation in [{'message':None},{'message':[]},{'message':{'content':'{}','role':'user'}},
                         {'message':{'content':'{}','tool_calls':[{}]}},{'done_reason':'length'},
                         {'error':'model unavailable'},{'model':'another-model'}]:
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):
                response=self.response('{"invoice_number":null}');response['model']=qp.MODEL
                response.update(mutation)
                validate_response(response,['invoice_number'],model=qp.MODEL)
        for raw in ('not JSON','{"done":true,"done":false}','{"value":NaN}'):
            with self.subTest(raw=raw),self.assertRaises(ValueError):parse_json(raw)

    def test_root_configuration_is_lazy_does_not_mutate_environment(self):
        with patch.dict(os.environ,{},clear=True),patch.object(qp,'dotenv_values',return_value={
                'OLLAMA_HOST':'http://localhost:11435/','OLLAMA_MODEL':'qwen2.5vl:7b',
                'OLLAMA_TIMEOUT_SECONDS':'45'}) as read:
            provider=OllamaQwenProvider(opener=Mock())
            read.assert_called_once_with(Path(__file__).resolve().parents[2]/'.env',interpolate=False)
            self.assertEqual((provider.base,provider.model,provider.timeout),('http://localhost:11435',qp.MODEL,45))
            self.assertNotIn('OLLAMA_HOST',os.environ)
        with patch.dict(os.environ,{'PYTHON_DOTENV_DISABLED':'1','OLLAMA_HOST':'http://localhost:11436',
                        'OLLAMA_MODEL':qp.MODEL,'OLLAMA_TIMEOUT_SECONDS':'25'},clear=True),patch.object(qp,'dotenv_values') as read:
            provider=OllamaQwenProvider(opener=Mock())
            self.assertEqual((provider.base,provider.timeout),('http://localhost:11436',25))
            read.assert_not_called()

    def test_invalid_configuration_never_opens_transport(self):
        for key,value in [('base','localhost:11434'),('base','http://user:secret@localhost:11434'),
                          ('base','http://localhost:11434/api'),('base','http://localhost:99999'),
                          ('model',''),('model','model name'),('timeout',0),('timeout','nan'),
                          ('timeout',301),('timeout',True)]:
            with self.subTest(key=key),self.assertRaises(ValueError):
                settings=dict(base=qp.BASE,model=qp.MODEL,timeout=30);settings[key]=value
                OllamaQwenProvider(opener=Mock(),**settings)

    def test_ollama_http_request_uses_configured_model_endpoint_and_timeout(self):
        opener=MagicMock();response=opener.open.return_value.__enter__.return_value
        response.read.return_value=b'{"version":"offline"}'
        provider=OllamaQwenProvider(opener,base='http://localhost:11435/',model='qwen2.5vl:7b',timeout=42)
        self.assertEqual(provider.metadata('/api/version'),{'version':'offline'})
        payload=prepare_request(b'image',['invoice_number'],model=provider.model)
        response.read.return_value=b'raw response'
        self.assertEqual(provider.chat(payload),'raw response')
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'http://localhost:11435/api/chat')
        self.assertEqual(request.get_method(),'POST')
        self.assertEqual(json.loads(request.data),payload)
        self.assertEqual(opener.open.call_args.kwargs['timeout'],42)
        payload['model']='wrong'
        with self.assertRaises(ValueError):provider.chat(payload)

    def test_profile_specific_names_and_excluded_operations(self):
        for profile,values in [('invoice',{'supplier_name':'Issuer','supplier_address':'Line one\nLine two','invoice_date':'2025-12-25'}),
                               ('bill',{'issuer':'Provider','account_number':'A-42','amount_due':'40.00','bill_date':'2025-12-25'}),
                               ('receipt',{'merchant':'Current shop','merchant_address':'Line one\nLine two','transaction_date':None})]:
            with self.subTest(profile=profile):
                request=prepare_request(b'image',list(values),profile=profile)
                self.assertEqual(set(request['format']['properties']),set(values))
                self.assertEqual(validate_response(self.response(json.dumps(values)),list(values),profile=profile),values)
        for profile,field in [('receipt','merchant_name'),('receipt','supplier_name'),('bill','supplier_name'),
                              ('invoice','issuer'),('invoice','currency'),('bill','tax_amount'),('receipt','payment_method'),
                              ('receipt','line_items'),('credit_note','total_amount')]:
            with self.subTest(profile=profile,field=field),self.assertRaises(ValueError):
                prepare_request(b'image',[field],profile=profile)

class SelectiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'extraction.json'
        self.pdf = self.root / 'missing_number.pdf'
        self.doc = self.root / 'document.json'
        self.pdf.write_bytes(b'synthetic bytes for mocked renderer')
        doc = {'schema_name': 'DoclingDocument', 'pages':{'1':{'size':{'width':595,'height':842}}},
               'texts': [{'text': text, 'label': 'title' if i==0 else 'text', 'self_ref':f'#/texts/{i}',
                         'prov':[{'page_no':1,'bbox':dict(l=20,t=20+i*60,r=400,b=40+i*60,coord_origin='TOPLEFT')}]}
                        for i,text in enumerate(('Invoice','Invoice number: X-42'))]}
        self.doc.write_text(json.dumps(doc))
        digest = hashlib.sha256(self.doc.read_bytes()).hexdigest()
        self.payload = fixture()
        classification = classify_document(doc)
        classification.update(source_document_json=str(self.doc), source_json_sha256=digest)
        self.payload['document_type'] = classification
        self.payload['provenance'] = {'source_document_json': str(self.doc), 'source_json_sha256': digest,
            'parser_run': {'status': 'success', 'source': str(self.pdf),
                           'source_sha256': hashlib.sha256(self.pdf.read_bytes()).hexdigest()}}
        self.provider = Mock()
        self.provider.base=qp.BASE;self.provider.model=qp.MODEL;self.provider.timeout=45
        self.provider.metadata.return_value = {'models': [{'name':qp.MODEL}]}
        self.provider.chat.return_value = json.dumps({'done': True, 'model':qp.MODEL,'message': {'content': '{"invoice_number":null}'}})
        self.factory = Mock(return_value=self.provider)
        self.renderer = Mock(side_effect=self.render)

    def render(self, pdf, target):
        target.write_bytes(b'synthetic rendered image bytes')
        return [100, 100]

    def missing(self):
        self.payload['fields']['invoice_number'].update(value=None, raw_text=None,
            state='missing', legacy_mapping_status='missing', evidence=[])
        self.payload['issues'].append({'code': 'UNMAPPED_FIELD', 'detail': 'invoice_number: missing'})

    def run_case(self):
        self.source.write_text(json.dumps(self.payload))
        return run_attempt(self.source, self.root, self.factory, self.renderer,
                           inference_permission=True,source_access_authorized=True,source_path=self.pdf,attempts={})

    def test_parser_only_makes_no_provider_or_render_call(self):
        out, record = self.run_case()
        self.assertEqual(record['status'], 'skipped_parser_only')
        self.factory.assert_not_called()
        self.renderer.assert_not_called()
        self.assertTrue((out/'run.json').exists())

    def test_manual_review_makes_no_provider_call(self):
        self.payload['issues'].append({'code': 'UNSUPPORTED_TABLE', 'detail': 'test'})
        _, record = self.run_case()
        self.assertEqual(record['status'], 'skipped_human_review')
        self.factory.assert_not_called()

    def test_missing_number_calls_only_requested_field_and_preserves_null(self):
        self.missing()
        before = json.dumps(self.payload)
        out, record = self.run_case()
        self.assertEqual(record['status'], 'response_structure_valid_needs_review')
        self.provider.chat.assert_called_once()
        request = self.provider.chat.call_args.args[0]
        self.assertEqual(request['format']['required'], ['invoice_number'])
        self.assertEqual(json.loads((out/'prediction.json').read_text()), {'invoice_number': None})
        self.assertEqual(self.source.read_text(), before)
        self.assertTrue(record['review_required'])

    def test_changed_pdf_blocks_provider(self):
        self.missing()
        self.pdf.write_bytes(b'changed')
        _, record = self.run_case()
        self.assertEqual(record['status'], 'failed')
        self.factory.assert_not_called()

    def test_changed_document_blocks_provider(self):
        self.missing()
        self.doc.write_text('{}')
        _, record = self.run_case()
        self.assertEqual(record['status'], 'failed')
        self.factory.assert_not_called()

    def test_timeout_is_saved_without_retry(self):
        self.missing()
        self.provider.chat.side_effect = TimeoutError('synthetic timeout')
        out, record = self.run_case()
        self.assertEqual(record['status'], 'failed')
        self.assertTrue(record['ai_request_attempted'])
        self.assertFalse(record['ai_response_received'])
        self.provider.chat.assert_called_once()
        self.assertIn('timeout', (out/'run.json').read_text())

    def test_malformed_raw_response_saved_before_validation(self):
        self.missing()
        self.provider.chat.return_value = 'not JSON'
        out, record = self.run_case()
        self.assertEqual(record['status'], 'failed')
        self.assertEqual((out/'raw_response.txt').read_text(), 'not JSON')
        self.assertFalse((out/'prediction.json').exists())

    def test_renderer_failure_blocks_provider(self):
        self.missing()
        self.renderer.side_effect = ValueError('multiple pages not supported')
        _, record = self.run_case()
        self.assertEqual(record['status'], 'failed')
        self.factory.assert_not_called()

    def test_heading_tampering_blocks_provider(self):
        self.missing()
        self.payload['document_type']['candidates'][0]['evidence']['text'] = 'false heading'
        _, record = self.run_case()
        self.assertEqual(record['status'], 'failed')
        self.factory.assert_not_called()

    def test_unavailable_provider_or_wrong_model_preserves_parser_without_chat(self):
        self.missing()
        for failure in ('connection','wrong_model','invalid_inventory','invalid_config'):
            with self.subTest(failure=failure):
                self.factory.side_effect=None;self.provider.metadata.side_effect=None
                self.provider.metadata.return_value={'models':[{'name':qp.MODEL}]}
                if failure=='connection':self.provider.metadata.side_effect=URLError('offline unavailable')
                elif failure=='wrong_model':self.provider.metadata.return_value={'models':[{'name':'wrong-model'}]}
                elif failure=='invalid_inventory':self.provider.metadata.return_value=[]
                else:self.factory.side_effect=ValueError('Invalid Ollama endpoint configuration')
                out,record=self.run_case()
                self.assertEqual(record['status'],'failed')
                self.assertFalse(record['ai_request_attempted'])
                self.assertTrue(record['review_required'])
                self.provider.chat.assert_not_called()
                self.assertFalse((out/'prediction.json').exists())
                self.assertEqual(json.loads(self.source.read_text()),self.payload)

    def test_bad_model_response_keeps_raw_bytes_without_prediction(self):
        self.missing()
        for response in [json.dumps({'done':True,'model':'wrong-model','message':{'content':'{"invoice_number":"X-42"}'}}),
                         '{"done":true,"done":true}',
                         json.dumps({'done':True,'model':qp.MODEL,'message':{'content':'{"invoice_number":"A","invoice_number":"B"}'}})]:
            with self.subTest(response=response):
                self.provider.chat.return_value=response
                out,record=self.run_case()
                self.assertEqual(record['status'],'failed')
                self.assertEqual((out/'raw_response.txt').read_text(),response)
                self.assertFalse((out/'prediction.json').exists())

    def test_candidate_reconciliation_preserves_hashes_nulls_and_unverified_evidence(self):
        from services.extraction.classification.reconcile_selective import reconcile
        self.missing()
        self.provider.chat.return_value=json.dumps({'done':True,'model':qp.MODEL,
                                                   'message':{'content':'{"invoice_number":"X-42"}'}},indent=2)+'\n'
        out,record=self.run_case()
        _,result=self.profiled_reconcile(out)
        field=result['fields']['invoice_number'];candidate=field['vlm']
        self.assertEqual(candidate['candidate'],'X-42');self.assertIsNone(field['final_value'])
        self.assertIsNone(candidate['confidence']);self.assertEqual(candidate['evidence'],[])
        self.assertEqual(candidate['provenance']['grounding'],'unverified_page_image')
        self.assertEqual(candidate['provenance']['image_sha256'],record['image_sha256'])
        self.assertEqual(candidate['provenance']['prediction_sha256'],record['prediction_sha256'])
        self.assertEqual(record['timeout_seconds'],45)
        self.assertEqual((out/'raw_response.txt').read_bytes(),self.provider.chat.return_value.encode('utf-8'))
        self.assertEqual(json.loads(self.source.read_text()),self.payload)
        raw=(out/'raw_response.txt').read_bytes()
        (out/'raw_response.txt').write_text('changed')
        with self.assertRaisesRegex(ValueError,'raw-response SHA-256'):self.profiled_reconcile(out)
        (out/'raw_response.txt').write_bytes(raw)
        (out/'prediction.json').write_text('{"invoice_number":"CHANGED"}')
        with self.assertRaisesRegex(ValueError,'prediction SHA-256'):self.profiled_reconcile(out)

    def test_real_renderer_supports_one_page_and_rejects_multipage_before_inference(self):
        from pypdf import PdfWriter
        for pages in (1,2):
            with self.subTest(pages=pages):
                writer=PdfWriter()
                for _ in range(pages):writer.add_blank_page(width=100,height=100)
                stream=BytesIO();writer.write(stream)
                if pages==1:
                    target=self.root/'one-page.png'
                    self.assertEqual(render_single_page(stream.getvalue(),target),[150,150])
                    self.assertTrue(target.read_bytes().startswith(b'\x89PNG'))
                else:
                    self.missing();self.pdf.write_bytes(stream.getvalue())
                    self.payload['provenance']['parser_run']['source_sha256']=hashlib.sha256(self.pdf.read_bytes()).hexdigest()
                    self.source.write_text(json.dumps(self.payload))
                    _,record=run_attempt(self.source,self.root,self.factory,inference_permission=True,
                        source_access_authorized=True,source_path=self.pdf,attempts={})
                    self.assertEqual(record['status'],'failed');self.assertFalse(record['ai_request_attempted'])
                    self.factory.assert_not_called()

    def profile_case(self,kind,field):
        from tests.extraction.test_financial_layout import layout
        from services.extraction.parsing.map_invoice import map_document
        from services.extraction.parsing.extraction_v1 import adapt_result
        title={'bill':'UTILITY BILL','receipt':'RECEIPT'}[kind]
        label={'bill':'Bill reference: B-42','receipt':'Merchant: Current Shop'}[kind]
        doc=layout(title,[(label,20,60,350,80),('Total: 40.00',20,140,350,160)])
        self.doc.write_text(json.dumps(doc));hashed=hashlib.sha256(self.doc.read_bytes()).hexdigest()
        mapped=map_document(doc);mapped['run_id']='profile-test'
        payload=adapt_result(mapped,'DOC-PROFILE','mapped.json','a'*64).model_dump()
        payload.update(schema_version='1.1.0-proposed',classification_attachment=self.payload['classification_attachment'],
                       document_type={**classify_document(doc),'source_document_json':str(self.doc),'source_json_sha256':hashed},
                       provenance={**self.payload['provenance'],'source_json_sha256':hashed})
        payload['fields'][field].update(state='missing',value=None,raw_text=None,evidence=[],candidates=[])
        self.payload=payload

    def profiled_attempt(self,fields,value=None,attempts=None,**kwargs):
        self.source.write_text(json.dumps(self.payload))
        self.provider.chat.return_value=json.dumps({'done':True,'model':qp.MODEL,
            'message':{'content':json.dumps({fields[0]:value})}})
        arguments=dict(source_path=self.pdf,source_access_authorized=True,selected_fields=fields,
                       inference_permission=True,attempts={} if attempts is None else attempts)
        arguments.update(kwargs)
        return run_attempt(self.source,self.root,self.factory,self.renderer,**arguments)

    def profiled_reconcile(self,out,**kwargs):
        from services.extraction.classification.reconcile_selective import reconcile
        args=dict(project_root=self.root,source_path=self.pdf,source_access_authorized=True);args.update(kwargs)
        return reconcile(self.source,out,self.root/'runs',**args)

    def test_invoice_bill_receipt_selected_missing_fields_reconcile_without_acceptance(self):
        for kind,field,value in [('invoice','invoice_number','X-42'),('bill','bill_reference','B-42'),('receipt','merchant','Current Shop')]:
            with self.subTest(kind=kind):
                if kind=='invoice':self.missing()
                else:self.profile_case(kind,field)
                before=json.dumps(self.payload,sort_keys=True)
                out,record=self.profiled_attempt([field],value)
                self.assertEqual(record['status'],'response_structure_valid_needs_review',record)
                self.assertEqual(record['profile'],kind)
                request=self.provider.chat.call_args.args[0]
                self.assertEqual(request['format']['required'],[field])
                _,result=self.profiled_reconcile(out)
                recovered=result['fields'][field]
                self.assertEqual(recovered['vlm']['candidate'],value);self.assertIsNone(recovered['final_value'])
                self.assertEqual(recovered['vlm']['verification']['status'],'unverified')
                self.assertEqual(recovered['vlm']['profile'],kind);self.assertEqual(recovered['vlm']['field'],field)
                self.assertIsNone(recovered['vlm']['confidence']);self.assertEqual(recovered['vlm']['evidence'],[])
                self.assertTrue(result['review_required']);self.assertFalse(result['safety']['ai_candidate_auto_accepted'])
                self.assertEqual(json.dumps(self.payload,sort_keys=True),before)
                if kind!='invoice':self.assertEqual(record['decision'],'human_review')

    def test_profiled_null_and_weak_resolved_contradiction_preserve_parser(self):
        self.missing();out,record=self.profiled_attempt(['invoice_number'])
        _,result=self.profiled_reconcile(out)
        self.assertEqual(result['status'],'unresolved_needs_review')
        self.assertIn('MODEL_RETURNED_NULL',result['fields']['invoice_number']['vlm']['verification']['reasons'])
        field=self.payload['fields']['invoice_number'];field.update(state='resolved',value='PARSER-42',raw_text='PARSER-42')
        self.payload['issues']=[{'code':'INCOMPLETE_EVIDENCE','detail':'invoice_number'}]
        out,record=self.profiled_attempt(['invoice_number'],'OTHER-42')
        self.assertEqual(record['status'],'response_structure_valid_needs_review',record)
        _,result=self.profiled_reconcile(out)
        self.assertEqual(result['fields']['invoice_number']['final_value'],'PARSER-42')
        self.assertEqual(result['fields']['invoice_number']['vlm']['candidate'],'OTHER-42')

    def test_profiled_permissions_unsupported_resolved_and_wrong_profile_fail(self):
        self.missing()
        for kwargs,fields in [({'inference_permission':False},['invoice_number']),
                              ({'source_access_authorized':False},['invoice_number']),
                              ({'attempts':[]},['invoice_number']),({},['merchant']),({},['currency']),
                              ({},['supplier_name']),({},['invoice_number','invoice_number'])]:
            with self.subTest(fields=fields,kwargs=kwargs):
                _,record=self.profiled_attempt(fields,**kwargs)
                self.assertEqual(record['status'],'failed');self.provider.chat.assert_not_called()
        self.profile_case('receipt','merchant');out,record=self.profiled_attempt(['merchant'],'Current Shop')
        record['profile']='invoice';(out/'run.json').write_text(json.dumps(record))
        with self.assertRaises(ValueError):self.profiled_reconcile(out)

    def test_profiled_budget_repeats_timeout_and_missing_model_do_not_retry(self):
        self.missing();attempts={}
        self.provider.chat.side_effect=TimeoutError('offline timeout')
        _,record=self.profiled_attempt(['invoice_number'],attempts=attempts)
        self.assertEqual(record['status'],'failed');self.assertTrue(record['ai_request_attempted'])
        self.assertEqual(len(attempts),1)
        _,record=self.profiled_attempt(['invoice_number'],attempts=attempts)
        self.assertIn('already reserved',record['error']);self.provider.chat.assert_called_once()
        _,record=self.profiled_attempt(['invoice_number'],attempts={'a':'a','b':'b','c':'c'})
        self.assertIn('budget exhausted',record['error']);self.provider.chat.assert_called_once()
        self.provider.chat.reset_mock();self.provider.metadata.return_value={'models':[]}
        _,record=self.profiled_attempt(['invoice_number'])
        self.assertEqual(record['status'],'failed');self.provider.chat.assert_not_called()

    def test_profiled_invalid_response_and_tampered_source_or_image_fail(self):
        self.missing()
        out,record=self.profiled_attempt(['invoice_number'],['A','B'])
        self.assertEqual(record['status'],'failed');self.assertFalse((out/'prediction.json').exists())
        out,record=self.profiled_attempt(['invoice_number'],'X-42')
        with self.assertRaises(ValueError):self.profiled_reconcile(out,source_access_authorized=False)
        for name in ('prompt.txt','request-settings.json'):
            path=out/name;original=path.read_bytes();path.write_bytes(b'changed request context')
            with self.subTest(artifact=name),self.assertRaises(ValueError):self.profiled_reconcile(out)
            path.write_bytes(original)
        (out/'page-1.png').write_bytes(b'changed image')
        with self.assertRaises(ValueError):self.profiled_reconcile(out)
        self.pdf.write_bytes(b'changed input');self.provider.chat.reset_mock()
        _,record=self.profiled_attempt(['invoice_number'],'X-42')
        self.assertEqual(record['status'],'failed');self.provider.chat.assert_not_called()

    def test_real_rec00002_current_former_merchant_reaches_mock_candidate_only(self):
        from services.extraction.classification.reconcile_selective import reconcile
        root=Path(__file__).resolve().parents[2]
        actual=root/'ai/runs/benchmark_v1_trusted_source_fresh_20261030_03'
        services=list(actual.glob('runs/document-intelligence-*/service-result.json'))
        if not services:self.skipTest('Existing source-backed development run is unavailable')
        summary=next(json.loads(p.read_text()) for p in services if json.loads(p.read_text())['document_id']=='REC00002')
        source=Path(summary['artifacts']['classified_extraction']);before=source.read_bytes()
        ext=json.loads(before);binding=json.loads(Path(ext['provenance']['source_transformation']['path']).read_text())
        original=Path(binding['original']['path'])
        with TemporaryDirectory(dir=root/'ai/runs') as directory:
            for code,field_name in [('INV00006','supplier_name'),('BIL00002','issuer')]:
                with self.subTest(protected=code):
                    protected=next(json.loads(p.read_text()) for p in services if json.loads(p.read_text())['document_id']==code)
                    protected_path=Path(protected['artifacts']['classified_extraction']);protected_bytes=protected_path.read_bytes()
                    protected_ext=json.loads(protected_bytes)
                    self.assertEqual(protected_ext['fields'][field_name]['state'],'resolved')
                    protected_binding=json.loads(Path(protected_ext['provenance']['source_transformation']['path']).read_text())
                    _,record=run_attempt(protected_path,root,self.factory,
                        source_path=Path(protected_binding['original']['path']),source_access_authorized=True,
                        selected_fields=[field_name],inference_permission=True,attempts={},output_parent=Path(directory))
                    self.assertEqual(record['status'],'failed');self.provider.chat.assert_not_called()
                    self.assertEqual(protected_path.read_bytes(),protected_bytes)
            for candidate in ('Gerbang Alaf Restaurants Sdn Bhd','Golden Arches Restaurants Sdn Bhd',None):
                with self.subTest(candidate=candidate):
                    self.provider.chat.return_value=json.dumps({'done':True,'model':qp.MODEL,
                        'message':{'content':json.dumps({'merchant':candidate})}})
                    out,record=run_attempt(source,root,self.factory,source_path=original,source_access_authorized=True,
                        selected_fields=['merchant'],inference_permission=True,attempts={},output_parent=Path(directory))
                    self.assertEqual(record['status'],'response_structure_valid_needs_review',record)
                    self.assertEqual(record['decision'],'human_review')
                    prompt=self.provider.chat.call_args.args[0]['messages'][0]['content']
                    self.assertIn('formerly known as',prompt)
                    self.assertIn('Gerbang Alaf Restaurants Sdn Bhd',prompt)
                    self.assertIn('Golden Arches Restaurants Sdn Bhd',prompt)
                    _,result=reconcile(source,out,Path(directory),project_root=root,source_path=original,source_access_authorized=True)
                    field=result['fields']['merchant']
                    self.assertEqual(field['parser']['state'],'ambiguous');self.assertEqual(len(field['parser']['candidates']),2)
                    self.assertEqual(field['vlm']['candidate'],candidate);self.assertIsNone(field['final_value'])
                    self.assertEqual(field['vlm']['verification']['status'],'unverified')
                    self.assertTrue(field['review_required'])
        self.assertEqual(source.read_bytes(),before)

if __name__ == '__main__':
    unittest.main()


