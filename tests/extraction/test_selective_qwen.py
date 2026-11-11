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
        # 25/12/2025 is now read as its one possible date; a day/month pair that could be swapped still fails.
        for field,value in [('invoice_date','04/05/2025'),('invoice_date','2025-02-30'),
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
                              ('invoice','issuer'),('invoice','currency'),('bill','currency'),('receipt','payment_method'),
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
                    self.assertEqual(render_single_page(stream.getvalue(),target),[209,209])  # 100 pt at 150 dpi.
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

class IndependentFieldValidationTests(unittest.TestCase):
    def test_one_bad_field_does_not_discard_the_others(self):
        usable,rejected=qp.validate_fields_independently({'invoice_date':'04/07/2023','total_amount':'10.00','invoice_number':'A-1'},
            ['invoice_date','total_amount','invoice_number'],'invoice')
        self.assertEqual(usable,{'invoice_date':None,'total_amount':'10.00','invoice_number':'A-1'})
        self.assertEqual(list(rejected),['invoice_date'])

    def test_a_missing_field_is_reported_not_invented(self):
        usable,rejected=qp.validate_fields_independently({'total_amount':'10.00'},['total_amount','invoice_number'],'invoice')
        self.assertEqual((usable['invoice_number'],rejected['invoice_number']),(None,'FIELD_MISSING_FROM_RESPONSE'))

    def test_nothing_usable_is_a_failed_response(self):
        with self.assertRaises(ValueError):
            qp.validate_fields_independently({'due_date':'On Receipt'},['due_date'],'bill')

    def test_all_null_is_a_valid_answer(self):
        self.assertEqual(qp.validate_fields_independently({'due_date':None},['due_date'],'bill'),({'due_date':None},{}))

    def test_unrequested_fields_fail_the_response(self):
        with self.assertRaises(ValueError):
            qp.validate_fields_independently({'total_amount':'1.00','currency':'USD'},['total_amount'],'invoice')

    def test_currency_is_not_a_recoverable_field_for_any_profile(self):
        for profile in ('invoice','bill','receipt'):
            with self.subTest(profile=profile),self.assertRaises(ValueError):
                qp.validate_fields_independently({'currency':'USD'},['currency'],profile)

    def test_mode_defaults_off_and_is_explicit(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(qp.qwen_mode(),'off')
        with patch.dict(os.environ,{'QWEN_RECOVERY_MODE':'manual'},clear=True):
            self.assertEqual(qp.qwen_mode(),'manual')
        with patch.dict(os.environ,{'ENABLE_QWEN_RECOVERY':'true'},clear=True):
            self.assertEqual(qp.qwen_mode(),'both')
        with patch.dict(os.environ,{'QWEN_RECOVERY_MODE':'off','ENABLE_QWEN_RECOVERY':'true'},clear=True):
            self.assertEqual(qp.qwen_mode(),'off')


class ArbitrationTests(unittest.TestCase):
    def test_every_outcome(self):
        from services.extraction.classification.reconcile_selective import arbitrate as a
        self.assertEqual(a('10.00','10.0','source_role_verified'),'PARSER_PRESERVED')
        self.assertEqual(a('10.00',None,'not_checked'),'PARSER_PRESERVED')
        self.assertEqual(a('10.00','12.00','source_role_verified'),'PARSER_MODEL_CONFLICT')
        self.assertEqual(a(None,'12.00','source_role_verified'),'SOURCE_VERIFIED_CANDIDATE')
        self.assertEqual(a(None,'12.00','source_text_match'),'UNVERIFIED_MODEL_SUGGESTION')
        self.assertEqual(a(None,'12.00','not_found_in_source'),'NO_RECOVERY')
        self.assertEqual(a(None,'2020-01-02','date_order_not_verifiable'),'NO_RECOVERY')
        self.assertEqual(a(None,None,'not_checked'),'NO_RECOVERY')
        self.assertEqual(a(None,None,'not_checked','bad format'),'MODEL_FAILURE')


class AIAssistTests(unittest.TestCase):
    """Real source checks and arbitration on a real pipeline run; only the model is faked."""
    def setUp(self):
        from pypdf import PdfWriter
        import services.extraction.workflow.document_extraction_service as dis
        from services.extraction.workflow.execution import execute_document_extraction
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'runs').mkdir()
        self.pdf=self.root/'upload.pdf'
        writer=PdfWriter();writer.add_blank_page(width=600,height=900)
        with self.pdf.open('wb') as handle:writer.write(handle)
        from tests.extraction.test_financial_layout import layout
        doc=layout('RECEIPT',[('Date: 2020-01-31',20,60,300,80),('Total: 10.60',20,120,300,140),('Merchant Shop Sdn Bhd',20,160,300,180),('12 Jalan Bayu',20,190,300,210)])
        harness=RecoveryEndToEndTests('test_off_by_default_no_model_is_called')
        result=execute_document_extraction('DOC-1',self.pdf,project_root=self.root,parser=harness.parser(doc))
        self.assertEqual(result['execution_status'],'EXTRACTED',result)
        self.run_id=result['service_run_id']
        self.provider=Mock(base=qp.BASE,model=qp.MODEL,timeout=45)
        self.provider.metadata.return_value={'models':[{'name':qp.MODEL,'digest':'sha256:local'}]}

    def assist(self,answer,fields=None,**kwargs):
        from services.extraction.ai.ai_assist import run_ai_assist
        self.provider.chat.return_value=json.dumps({'done':True,'model':qp.MODEL,'message':{'content':json.dumps(answer)}})
        def render(pdf,target):
            target.write_bytes(b'page');return [100,100]
        return run_ai_assist(self.run_id,self.pdf,fields,root=self.root,provider_factory=Mock(return_value=self.provider),renderer=render,**kwargs)

    def by_field(self,result):
        return {row['field']:row for row in result['fields']}

    def test_verified_candidate_is_advice_with_evidence(self):
        result=self.assist({'transaction_time':None,'tax_amount':'10.60'},['transaction_time','tax_amount'])
        row=self.by_field(result)['tax_amount']
        self.assertEqual((row['outcome'],row['source_check'],row['candidate']),('UNVERIFIED_MODEL_SUGGESTION','source_text_match','10.60'))
        self.assertEqual(self.by_field(result)['transaction_time']['outcome'],'NO_RECOVERY')
        result=self.assist({'total_amount':'10.60','merchant_address':None},['total_amount','merchant_address'])
        self.assertEqual(self.by_field(result)['total_amount']['source_check'],'source_role_verified')
        self.assertEqual(result['model_digest'],'sha256:local')
        self.assertFalse(result['accounting_approval_performed'])

    def test_hallucination_is_rejected_and_its_value_not_returned(self):
        result=self.assist({'merchant_address':'99 Invented Road'},['merchant_address'])
        row=self.by_field(result)['merchant_address']
        self.assertEqual((row['outcome'],row['candidate'],row['source_check']),('NO_RECOVERY',None,'not_found_in_source'))
        self.assertNotIn('Invented',json.dumps(result))

    def test_second_opinion_never_changes_the_parser_value(self):
        row=self.by_field(self.assist({'total_amount':'99.00'},['total_amount']))['total_amount']
        self.assertEqual(row['parser_value'],'10.60')
        self.assertEqual(row['candidate'],None)
        row=self.by_field(self.assist({'total_amount':'10.60'},['total_amount']))['total_amount']
        self.assertEqual(row['outcome'],'PARSER_PRESERVED')

    def test_human_correction_is_compared_not_replaced(self):
        row=self.by_field(self.assist({'total_amount':'10.60'},['total_amount'],current_values={'total_amount':'11.00'}))['total_amount']
        self.assertEqual((row['current_value'],row['outcome']),('11.00','PARSER_MODEL_CONFLICT'))

    def test_currency_cannot_be_requested(self):
        from services.extraction.ai.ai_assist import AssistUnavailable
        with self.assertRaises(AssistUnavailable) as caught:
            self.assist({},['currency'])
        self.assertEqual(caught.exception.code,'FIELD_NOT_SUPPORTED')
        self.provider.chat.assert_not_called()

    def test_stopped_model_gives_a_plain_explanation(self):
        from services.extraction.ai.ai_assist import AssistUnavailable
        self.provider.metadata.side_effect=URLError('refused')
        with self.assertRaises(AssistUnavailable) as caught:
            self.assist({'merchant':None},['merchant'])
        self.assertEqual(caught.exception.code,'MODEL_UNAVAILABLE')

    def test_malformed_answer_reports_failure_and_changes_nothing(self):
        from services.extraction.ai.ai_assist import run_ai_assist
        self.provider.chat.return_value=json.dumps({'done':True,'model':qp.MODEL,'message':{'content':'not json'}})
        def render(pdf,target):
            target.write_bytes(b'page');return [100,100]
        result=run_ai_assist(self.run_id,self.pdf,['merchant'],root=self.root,provider_factory=Mock(return_value=self.provider),renderer=render)
        self.assertEqual(result['status'],'MODEL_FAILURE')
        self.assertEqual(result['fields'][0]['outcome'],'MODEL_FAILURE')

    def test_busy_model_is_refused_not_queued_up(self):
        from services.extraction.ai import ai_assist
        ai_assist._gpu_slot.acquire()
        try:
            with self.assertRaises(ai_assist.AssistUnavailable) as caught:
                self.assist({'merchant':None},['merchant'])
            self.assertEqual(caught.exception.code,'MODEL_BUSY')
        finally:
            ai_assist._gpu_slot.release()


class RegistryTests(unittest.TestCase):
    def test_every_recoverable_field_can_be_saved_by_a_person(self):
        from services.extraction.classification.route_extraction import RECOVERY_FIELDS
        from services.extraction.contracts.accounting_contract import FIELD_KEYS
        from services.extraction.contracts.field_catalog import CANONICAL_FIELD_TYPES
        import main  # noqa: F401  (route modules load through the app)
        from services.routes.documents.routes import CORRECTABLE_FIELDS
        for profile,names in RECOVERY_FIELDS.items():
            for name in names:
                with self.subTest(profile=profile,field=name):
                    self.assertIn(FIELD_KEYS[name],CORRECTABLE_FIELDS)
                    self.assertIn(name,qp.FIELD_DEFINITIONS)

    def test_placeholders_only_add_empty_cells_for_the_profile(self):
        from services.extraction.parsing.map_invoice import map_document,with_recovery_placeholders
        from tests.extraction.test_financial_layout import layout
        result=with_recovery_placeholders(map_document(layout('RECEIPT',[('Total: 10.60',20,120,300,140)])))
        self.assertEqual(result['fields']['merchant_address']['value'],None)
        self.assertEqual(result['fields']['merchant_address']['mapping_status'],'missing')
        self.assertEqual(result['fields']['total_amount']['value'],'10.60')
        self.assertNotIn('supplier_address',result['fields'])


class RunnerEquivalenceTests(unittest.TestCase):
    """The plain runner and the graph share stages; both must publish the same result."""
    def run_mode(self,graph,**provider):
        harness=RecoveryEndToEndTests('test_off_by_default_no_model_is_called');harness.setUp()
        self.addCleanup(harness.doCleanups)
        harness.reply('12.00')
        for key,value in provider.items():setattr(harness.provider.chat,key,value)
        with patch.dict(os.environ,{'ENABLE_EXTRACTION_GRAPH':'true' if graph else 'false'}):
            public=harness.execute(**harness.enabled)
        summary=json.loads(next((harness.root/'runs').glob('document-intelligence-*/service-result.json')).read_text())
        return public,summary

    @staticmethod
    def comparable(public):
        result=json.loads(json.dumps(public))
        result.pop('provenance')
        def strip(value):
            if isinstance(value,dict):
                return {k:strip(v) for k,v in value.items() if k not in {'attempt_id','model_digest','runner','stage_seconds','elapsed_seconds'}}  # Measurements differ run to run.
            if isinstance(value,list):
                return [strip(v) for v in value]
            return value
        return strip(result)

    def test_plain_runner_records_stages_without_values(self):
        _,summary=self.run_mode(False)
        self.assertEqual(summary['orchestration']['runner'],'plain')
        names=[s['stage'] for s in summary['stage_trace']]
        self.assertEqual(names,['verify_source','primary_extract','classify_and_map','assess_field_evidence','select_recovery','run_qwen_recovery','reconcile_candidates','precheck_validation','final_route'])
        self.assertNotIn('12.00',json.dumps(summary['stage_trace']))

    def run_with_failures(self,*chat):
        harness=RecoveryEndToEndTests('test_off_by_default_no_model_is_called');harness.setUp()
        self.addCleanup(harness.doCleanups)
        harness.reply('12.00');good=harness.provider.chat.return_value
        bad=json.dumps({'done':True,'model':qp.MODEL,'message':{'content':'{not json'}})
        harness.provider.chat.side_effect=[{'good':good,'bad':bad}.get(item,item) for item in chat]
        public=harness.execute(**harness.enabled)
        summary=json.loads(next((harness.root/'runs').glob('document-intelligence-*/service-result.json')).read_text())
        return public,summary,harness.provider.chat.call_count

    def test_failed_attempt_is_retried_once_through_verification(self):
        public,summary,calls=self.run_with_failures('bad','good')
        self.assertEqual(calls,2)
        self.assertEqual(summary['targeted_retry']['result'],'retry_used')
        self.assertEqual(summary['targeted_retry']['needed_fields'],['total_amount'])
        names=[s['stage'] for s in summary['stage_trace']]
        self.assertEqual(names.count('targeted_retry'),1)
        self.assertLess(names.index('targeted_retry'),len(names)-names[::-1].index('reconcile_candidates')-1)
        self.assertEqual(names[-1],'final_route')
        self.assertIn('12.00',[c.get('value') for c in public['fields']['total_amount']['candidates']])
        self.assertIsNone(public['fields']['total_amount']['value'])

    def test_second_failure_stops_and_keeps_the_first_outcome(self):
        public,summary,calls=self.run_with_failures('bad','bad','good')
        self.assertEqual(calls,2)
        self.assertEqual(summary['targeted_retry']['result'],'first_attempt_kept')
        self.assertEqual(summary['status'],'ai_attempt_failed_needs_review')
        self.assertEqual(public['status'],'ai_attempt_failed_needs_review')
        self.assertEqual(summary['final_route'],'ACCOUNTANT_ACTION_REQUIRED')

    def test_precheck_failure_never_stops_the_extraction(self):
        import builtins
        real=builtins.__import__
        def blocked(name,*args,**kwargs):
            if name.startswith('validation'):
                raise ImportError('blocked',name=name)
            return real(name,*args,**kwargs)
        with patch('builtins.__import__',blocked):
            public,summary=self.run_mode(False)
        self.assertEqual(summary['validation_precheck']['status'],'unavailable')
        self.assertEqual(summary['final_route'],'ACCOUNTANT_CONFIRMATION' if not public['fields']['total_amount']['candidates'] else summary['final_route'])
        self.assertIn('final_route',[s['stage'] for s in summary['stage_trace']])
        self.assertIn('processing_trace',public)
        self.assertNotIn('12.00',json.dumps(public['processing_trace']))

    def test_successful_first_attempt_is_not_repeated(self):
        _,summary,calls=self.run_with_failures('good','good')
        self.assertEqual(calls,1)
        self.assertNotIn('targeted_retry',summary)

    def test_graph_falls_back_to_plain_when_not_installed(self):
        import builtins
        real=builtins.__import__
        def blocked(name,*args,**kwargs):
            if name.startswith('langgraph'):
                raise ImportError('blocked',name='langgraph')
            return real(name,*args,**kwargs)
        with patch('builtins.__import__',blocked):
            _,summary=self.run_mode(True)
        self.assertEqual(summary['orchestration']['runner'],'plain')
        self.assertIn('LANGGRAPH_NOT_INSTALLED',summary['orchestration']['reason'])

    def test_graph_matches_plain_runner(self):
        try:
            import langgraph  # noqa: F401
        except ImportError:
            self.skipTest('LangGraph is optional and not installed in this environment')
        plain,_=self.run_mode(False)
        graph,summary=self.run_mode(True)
        self.assertEqual(summary['orchestration']['runner'],'langgraph')
        self.assertEqual(self.comparable(plain),self.comparable(graph))

    def test_graph_matches_plain_runner_on_model_failure(self):
        try:
            import langgraph  # noqa: F401
        except ImportError:
            self.skipTest('LangGraph is optional and not installed in this environment')
        plain,_=self.run_mode(False,side_effect=TimeoutError('slow'))
        graph,_=self.run_mode(True,side_effect=TimeoutError('slow'))
        self.assertEqual(plain['status'],'ai_attempt_failed_needs_review')
        self.assertEqual(self.comparable(plain),self.comparable(graph))


class RecoveryLatencyBudgetTests(unittest.TestCase):
    """A model call may only use what is left of the 20 s per-document target."""
    def harness(self):
        harness=RecoveryEndToEndTests('test_off_by_default_no_model_is_called');harness.setUp()
        self.addCleanup(harness.doCleanups);harness.reply('12.00')
        return harness

    def summary(self,harness):
        return json.loads(next((harness.root/'runs').glob('document-intelligence-*/service-result.json')).read_text())

    def test_seconds_left_uses_the_deadline_and_rejects_bad_settings(self):
        from services.extraction.workflow import document_extraction_service as dis
        with patch.dict(os.environ,{},clear=False):
            os.environ.pop('QWEN_RECOVERY_DEADLINE_SECONDS',None)
            self.assertEqual(dis.recovery_seconds_left(9.0),20.0-9.0-dis.RECOVERY_POST_CALL_RESERVE_SECONDS)
        for bad in ('abc','nan','inf','1','1000'):
            with self.subTest(bad=bad),patch.dict(os.environ,{'QWEN_RECOVERY_DEADLINE_SECONDS':bad}):
                self.assertEqual(dis.recovery_seconds_left(0.0),20.0-dis.RECOVERY_POST_CALL_RESERVE_SECONDS)
        with patch.dict(os.environ,{'QWEN_RECOVERY_DEADLINE_SECONDS':'30'}):
            self.assertEqual(dis.recovery_seconds_left(0.0),30.0-dis.RECOVERY_POST_CALL_RESERVE_SECONDS)

    def test_exhausted_budget_sends_nothing_and_keeps_the_parser_result(self):
        from services.extraction.workflow import document_extraction_service as dis
        harness=self.harness()
        with patch.object(dis,'recovery_seconds_left',return_value=dis.RECOVERY_MIN_CALL_SECONDS-0.1):
            public=harness.execute(**harness.enabled)
        harness.provider.chat.assert_not_called()
        summary=self.summary(harness)
        self.assertEqual(summary['recovery_failure'],'LATENCY_BUDGET_EXHAUSTED')
        self.assertEqual(summary['status'],'ai_attempt_failed_needs_review')
        self.assertEqual(public['status'],'ai_attempt_failed_needs_review')
        self.assertIsNone(public['fields']['total_amount']['value'])
        self.assertNotIn('selective_run',summary['artifacts'])
        self.assertEqual(list((harness.root/'runs').rglob('selective-*')),[])
        self.assertTrue(public['review_required']);self.assertFalse(public['accounting_approval_performed'])

    def test_call_timeout_is_capped_by_the_seconds_left(self):
        from services.extraction.workflow import document_extraction_service as dis
        harness=self.harness();self.assertEqual(harness.provider.timeout,45)
        with patch.object(dis,'recovery_seconds_left',return_value=6.5):
            harness.execute(**harness.enabled)
        harness.provider.chat.assert_called_once()
        self.assertEqual(harness.provider.timeout,6.5)
        run=json.loads(next((harness.root/'runs').rglob('selective-*/run.json')).read_text())
        self.assertEqual(run['timeout_seconds'],6.5)

    def test_cap_never_raises_a_shorter_configured_timeout(self):
        from services.extraction.workflow import document_extraction_service as dis
        harness=self.harness();harness.provider.timeout=3
        with patch.object(dis,'recovery_seconds_left',return_value=15.0):
            harness.execute(**harness.enabled)
        self.assertEqual(harness.provider.timeout,3)


class AIAssistRouteTests(unittest.TestCase):
    """Route guards with a fake database. Real-Postgres runs of these are reported separately."""
    USER={'id':'u1','tenant_id':'t1','role':'accountant','name':'Reviewer'}

    def setUp(self):
        from datetime import datetime,timezone
        import main  # noqa: F401
        from services.routes.documents import routes
        self.routes=routes
        self.when=datetime(2026,11,2,tzinfo=timezone.utc)
        self.document={'id':'d1','status':'exception','kind':'bill','content_revision':3,'tenant_id':'t1'}
        self.recent=0;self.inserts=[];self.latest=None;self.decisions=[]
        outer=self
        class Rows:
            def __init__(self,rows):self.rows=rows
            def fetchone(self):return self.rows[0] if self.rows else None
            def fetchall(self):return self.rows
        class Db:
            def __enter__(self):return self
            def __exit__(self,*a):return False
            def execute(self,sql,params=()):
                if sql.lstrip().startswith('INSERT INTO audit_logs'):
                    outer.inserts.append(params);return Rows([])
                if 'FROM documents d' in sql:
                    return Rows([outer.document] if outer.document else [])
                if 'count(*)' in sql:return Rows([{'n':outer.recent}])
                if 'FROM raw_extractions' in sql:return Rows([{'service_run_id':'run-1'}])
                if 'FROM field_predictions' in sql:return Rows([])
                if 'FROM audit_logs' in sql and 'LIMIT 1' in sql:
                    return Rows([{'details':json.dumps(outer.latest),'created_at':outer.when,'user_name':'Reviewer'}] if outer.latest else [])
                if 'FROM audit_logs' in sql:return Rows(outer.decisions)
                raise AssertionError('Unexpected SQL: '+sql)
        for patcher in (patch.object(routes,'connect',return_value=Db()),patch.object(routes,'require_workspace_user',return_value=self.USER)):
            patcher.start();self.addCleanup(patcher.stop)

    def call(self,fields=None,env='both',**assist):
        from main import AIAssistInput
        with patch.dict(os.environ,{'QWEN_RECOVERY_MODE':env}),\
             patch('services.queue.jobs.resolve_document_from_database',return_value=Path(__file__)),\
             patch('services.extraction.ai.ai_assist.run_ai_assist',**assist) as run:
            return self.routes.request_ai_assist('d1',AIAssistInput(fields=fields),Mock()),run

    def assert_refused(self,status,**kwargs):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as caught:
            self.call(**kwargs)
        self.assertEqual(caught.exception.status_code,status)
        self.assertEqual(self.inserts,[])

    def test_mode_off_refuses_without_touching_the_model(self):
        self.assert_refused(409,env='off',return_value={})

    def test_auto_mode_does_not_enable_the_manual_button(self):
        self.assert_refused(409,env='auto',return_value={})

    def test_document_outside_tenant_or_ownership_is_not_found(self):
        self.document=None
        self.assert_refused(404,return_value={})

    def test_finished_or_processing_documents_are_refused(self):
        for status in ('standardised','completed','processing','queued'):
            with self.subTest(status=status):
                self.document={'status':status,'id':'d1','kind':'bill','content_revision':3,'tenant_id':'t1'}
                self.assert_refused(409,return_value={})

    def test_rate_limit_refuses_before_inference(self):
        self.recent=self.routes.AI_ASSIST_LIMIT_PER_10_MIN
        self.assert_refused(429,return_value={})

    def test_result_is_audited_and_changes_no_field(self):
        result,run=self.call(fields=['total_amount'],return_value={'status':'COMPLETED','fields':[{'field':'total_amount','outcome':'PARSER_MODEL_CONFLICT'}]})
        self.assertEqual(result['content_revision'],3)
        self.assertEqual(len(self.inserts),1)
        self.assertEqual(self.inserts[0][4],self.routes.AI_ASSIST_ACTION)
        self.assertEqual(json.loads(self.inserts[0][6])['requested_fields'],['total_amount'])
        run.assert_called_once()

    def test_model_unavailable_returns_an_explanation_not_an_error(self):
        from services.extraction.ai.ai_assist import AssistUnavailable
        result,_=self.call(side_effect=AssistUnavailable('MODEL_UNAVAILABLE','The local model is not responding.'))
        self.assertEqual((result['status'],result['fields']),('MODEL_UNAVAILABLE',[]))
        self.assertIn('not responding',result['message'])

    def test_a_result_stored_by_an_older_version_still_reads(self):
        self.latest={'status':'COMPLETED','fields':[{'field':'merchant','outcome':'NO_RECOVERY'}]}
        state=self.routes.get_ai_assist('d1',Mock())
        self.assertEqual(state['latest']['fields'][0]['stored_key'],'supplier')
        self.assertIn('provider_address',[f['storedKey'] for f in state['supportedFields']])

    def test_decision_is_recorded_without_changing_a_value(self):
        from main import AIAssistDecisionInput
        self.assertEqual(self.routes.decide_ai_assist('d1',AIAssistDecisionInput(field='issuer',decision='rejected',attemptId='a'),Mock()),{'recorded':True})
        self.assertEqual(json.loads(self.inserts[0][6]),{'field':'issuer','decision':'rejected','attempt_id':'a'})


class LocalOnlyProviderTests(unittest.TestCase):
    def test_only_this_machine_can_receive_documents(self):
        for base in ('http://127.0.0.1:11434','http://localhost:11434','http://[::1]:11434'):
            with self.subTest(base=base):
                self.assertEqual(OllamaQwenProvider(opener=Mock(),base=base,model=qp.MODEL,timeout=5).base,base)
        for base in ('https://api.example.com','http://10.0.0.5:11434','http://0.0.0.0:11434','http://ollama.example:11434'):
            with self.subTest(base=base),self.assertRaises(ValueError):
                OllamaQwenProvider(opener=Mock(),base=base,model=qp.MODEL,timeout=5)

    def test_default_opener_does_not_follow_redirects_off_the_machine(self):
        from urllib.request import HTTPRedirectHandler
        provider=OllamaQwenProvider(base=qp.BASE,model=qp.MODEL,timeout=5)
        handlers=[h for h in provider.opener.handlers if isinstance(h,HTTPRedirectHandler)]
        self.assertEqual(len(handlers),1)
        self.assertIsNone(handlers[0].redirect_request(None,None,301,'moved',{}, 'https://elsewhere.example'))

    def test_model_digest_comes_from_the_local_inventory(self):
        self.assertEqual(qp.model_digest({'models':[{'name':qp.MODEL,'digest':'abc'}]},qp.MODEL),'abc')
        self.assertIsNone(qp.model_digest({'models':[{'name':'other','digest':'abc'}]},qp.MODEL))


class SourceCheckTests(unittest.TestCase):
    def doc(self,*lines):
        return {'schema_name':'DoclingDocument','pages':{'1':{'size':{'width':600,'height':900}}},'texts':[],'m2_text_lines':[
            {'text':text,'self_ref':f'#/m2_text_lines/{i}','from_ocr':True,'ocr_score':.9,
             'prov':[{'page_no':1,'bbox':dict(l=20,t=20+i*30,r=400,b=40+i*30,coord_origin='TOPLEFT')}]}
            for i,text in enumerate(lines)]}

    def check(self,field,value,*lines):
        from services.extraction.classification.reconcile_selective import check_candidate_against_source
        return check_candidate_against_source(field,value,self.doc(*lines))

    def test_amounts_compare_numerically_across_printed_formats(self):
        for printed in ('$ 797,91','797.91','USD 797,91'):
            with self.subTest(printed=printed):
                self.assertEqual(self.check('total_amount','797.91',printed)['status'],'source_text_match')
        self.assertEqual(self.check('total_amount','797.91','Total 797.91')['status'],'source_role_verified')
        self.assertEqual(self.check('total_amount','11794.44','$ 11 794,44')['status'],'source_text_match')

    def test_invented_amount_is_rejected(self):
        self.assertEqual(self.check('total_amount','900.00','$ 797,91','72,54')['status'],'not_found_in_source')

    def test_model_time_notation_is_normalized_and_nothing_else(self):
        from services.extraction.ai.qwen_provider import _check_value, normalize_model_value as normal
        for raw,expected in (('11:05:16 AM','11:05:16'),('6:41:02','06:41:02'),('05:09:21 PM','17:09:21'),('12:05 am','00:05'),
                             ('12:05:00 p.m.','12:05:00'),('2019-01-15T11:05:16','11:05:16'),(' 17:09 ','17:09')):
            with self.subTest(raw=raw): self.assertEqual(normal('transaction_time',raw),expected)
        for raw in ('On Receipt','25:00','13:00 PM','11:75:00','noon','6'):
            with self.subTest(raw=raw):
                self.assertEqual(normal('transaction_time',raw),raw)
                with self.assertRaises(ValueError): _check_value('transaction_time',raw)
        self.assertEqual(normal('due_date','6:41:02'),'6:41:02')

    def test_printed_meridiem_decides_a_bare_model_time(self):
        printed=self.check('transaction_time','06:41:02','Date : 20/03/2018 6:41:02 PM')
        self.assertEqual((printed['status'],printed['printed_value']),('source_role_verified','18:41:02'))
        self.assertEqual(printed['evidence'][0]['text'],'Date : 20/03/2018 6:41:02 PM')
        self.assertEqual(self.check('transaction_time','17:09:21','Time 05:09:21 PM')['status'],'source_role_verified')
        self.assertNotIn('printed_value',self.check('transaction_time','11:05:16','Time 11:05:16 AM'))
        self.assertNotIn('printed_value',self.check('transaction_time','06:41:02','06:41:02'))

    def test_oversized_context_is_omitted_not_a_failed_attempt(self):
        import tempfile
        from pathlib import Path
        from services.extraction.ai.run_selective_qwen import recovery_context
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve(); (root/'document.json').write_text(json.dumps(self.doc('Total 27.35')))
            extraction={'provenance':{'source_document_json':str(root/'document.json')},
                        'fields':{'merchant':{'state':'missing','raw_text':'x'*7000,'value':None,'candidates':[]}}}
            plan={'fields':{'merchant':{'source_refs':[]}}}
            context=recovery_context(extraction,plan,['merchant'],root)
        self.assertEqual(context,{'page':1,'fields':{},'nearby_text':[],'omitted':'CONTEXT_EXCEEDS_BOUND'})

    def test_time_is_found_when_ocr_runs_the_date_into_it(self):
        self.assertEqual(self.check('transaction_time','17:09:21','ORD #90 -REG#19- 18/01/201817:09:21')['status'],'source_role_verified')

    def test_invented_or_partial_time_is_not_in_source(self):
        for value,line in (('06:41:03','6:41:02 PM'),('06:41','6:41:02 PM'),('18:00','Total 18.00'),('11:05:16','Tel 411:05:16')):
            with self.subTest(value=value): self.assertEqual(self.check('transaction_time',value,line)['status'],'not_found_in_source')

    def test_two_printed_readings_of_one_clock_are_not_guessed(self):
        self.assertEqual(self.check('transaction_time','06:41:02','In 6:41:02 AM','Out 6:41:02 PM')['status'],'not_found_in_source')

    def test_printed_placeholder_is_not_a_value(self):
        from services.extraction.classification.reconcile_selective import arbitrate
        for value in ('N/A','n/a',' N / A ','NA','n.a.','None','-','Not applicable'):
            with self.subTest(value=value):
                self.assertEqual(self.check('account_number',value,'Account number: N/A')['status'],'placeholder_not_a_value')
                self.assertEqual(arbitrate(None,value,'placeholder_not_a_value'),'NO_RECOVERY')
        self.assertEqual(self.check('account_number','NA-1042','Account number: NA-1042')['status'],'source_role_verified')

    def test_identifier_must_be_a_whole_token(self):
        self.assertEqual(self.check('invoice_number','89969473','Invoice no: 89969473')['status'],'source_role_verified')
        self.assertEqual(self.check('invoice_number','89969473','89969473')['status'],'source_text_match')
        self.assertEqual(self.check('invoice_number','8996','Invoice no: 89969473')['status'],'not_found_in_source')

    def test_party_text_tolerates_spacing_but_not_a_different_name(self):
        self.assertEqual(self.check('supplier_name','Johnson-Martin','Johnson Martin')['status'],'source_role_verified')
        self.assertEqual(self.check('supplier_name','Acme Holdings','Johnson-Martin')['status'],'not_found_in_source')

    def test_every_address_line_must_be_printed(self):
        printed=('3836 Moore Ports','North Michael, MO 01844')
        self.assertEqual(self.check('supplier_address','3836 Moore Ports\nNorth Michael, MO 01844',*printed)['status'],'source_text_match')
        anchored={'supplier_name':{'evidence':[{'source_ref':'#/m2_text_lines/0'}]}}
        from services.extraction.classification.reconcile_selective import check_candidate_against_source
        doc=self.doc('Johnson-Martin',*printed)
        self.assertEqual(check_candidate_against_source('supplier_address','3836 Moore Ports\nNorth Michael, MO 01844',doc,anchored)['status'],'source_role_verified')
        self.assertEqual(self.check('supplier_address','3836 Moore Ports\nSomewhere Else',*printed)['status'],'not_found_in_source')

    def test_day_month_order_cannot_be_confirmed_by_the_model(self):
        self.assertEqual(self.check('invoice_date','2017-04-01','04/01/2017')['status'],'date_order_not_verifiable')
        self.assertEqual(self.check('invoice_date','2017-01-04','04/01/2017')['status'],'date_order_not_verifiable')
        self.assertEqual(self.check('invoice_date','2016-10-29','10/29/2016')['status'],'source_text_match')
        self.assertEqual(self.check('invoice_date','2018-01-01','10/29/2016')['status'],'not_found_in_source')

    def test_neighbouring_total_cannot_verify_a_different_financial_role(self):
        for label in ('Amount Due', 'Current Charges', 'Balance Due', 'Cash', 'Sub Total'):
            with self.subTest(label=label):
                result = self.check('total_amount', '12.00', 'Grand Total 10.00', f'{label} 12.00')
                self.assertEqual(result['status'], 'source_text_match')

    def test_every_address_part_needs_the_same_party_role(self):
        from services.extraction.classification.reconcile_selective import check_candidate_against_source
        doc = self.doc('Example Supplier', '14 Seller Road', 'Bill To:', '78 Buyer Street')
        anchored = {'supplier_name': {'evidence': [{'source_ref': '#/m2_text_lines/0'}]}}
        result = check_candidate_against_source('supplier_address', '14 Seller Road\n78 Buyer Street', doc, anchored)
        self.assertEqual(result['status'], 'source_text_match')
        self.assertEqual(len(result['source_refs']), 2)

    def test_former_party_label_does_not_verify_current_merchant(self):
        for label in ('Formerly known as:', 'Previously:', 'Licensee of:'):
            with self.subTest(label=label):
                self.assertEqual(self.check('merchant', 'Old Trading', label + ' Old Trading')['status'], 'source_text_match')
                self.assertEqual(self.check('merchant', 'Old Trading', label, 'Old Trading')['status'], 'source_text_match')

    def test_value_under_the_wrong_label_stays_unverified(self):
        for label in ('Change','Cash','Sub Total','GST','Discount'):
            with self.subTest(label=label):
                self.assertEqual(self.check('total_amount','10.00',f'{label} 10.00')['status'],'source_text_match')
        self.assertEqual(self.check('invoice_date','2020-01-31','Due date: 2020-01-31')['status'],'source_text_match')
        self.assertEqual(self.check('tax_amount','6.00','GST ID No: 6.00')['status'],'source_text_match')

    def test_label_above_or_beside_a_value_supports_its_role(self):
        doc={'schema_name':'DoclingDocument','pages':{'1':{'size':{'width':600,'height':900}}},'texts':[],'m2_text_lines':[
            {'text':t,'self_ref':f'#/m2_text_lines/{i}','prov':[{'page_no':1,'bbox':dict(l=l,t=tp,r=r,b=b,coord_origin='TOPLEFT')}]}
            for i,(t,l,tp,r,b) in enumerate([('Grand Total',20,500,120,520),('10.00',300,500,380,520),('Tax',20,540,80,560),('Amount Due',300,600,400,620),('55.00',300,625,400,645)])]}
        from services.extraction.classification.reconcile_selective import check_candidate_against_source as check
        self.assertEqual(check('total_amount','10.00',doc)['status'],'source_role_verified')
        self.assertEqual(check('amount_due','55.00',doc)['status'],'source_role_verified')

    def test_customer_block_is_not_the_merchant(self):
        doc={'schema_name':'DoclingDocument','pages':{'1':{'size':{'width':600,'height':900}}},'texts':[],'m2_text_lines':[
            {'text':t,'self_ref':f'#/m2_text_lines/{i}','prov':[{'page_no':1,'bbox':dict(l=20,t=tp,r=300,b=tp+20,coord_origin='TOPLEFT')}]}
            for i,(t,tp) in enumerate([('Real Merchant Sdn Bhd',20),('Bill To:',60),('Customer Name Sdn Bhd',85)])]}
        from services.extraction.classification.reconcile_selective import check_candidate_against_source as check
        self.assertEqual(check('merchant','Real Merchant Sdn Bhd',doc)['status'],'source_role_verified')
        self.assertEqual(check('merchant','Customer Name Sdn Bhd',doc)['status'],'source_text_match')

    def test_match_carries_real_source_geometry(self):
        result=self.check('total_amount','797.91','$ 797,91')
        self.assertEqual(result['source_refs'],['#/m2_text_lines/0'])
        self.assertEqual(result['evidence'][0]['evidence_origin'],'parser_text_matched_to_model_candidate')
        self.assertTrue(result['evidence'][0]['bbox'])


class RecoveryEndToEndTests(unittest.TestCase):
    """Real mapper, routing, reconciliation and publication; only the model is faked."""
    def setUp(self):
        from pypdf import PdfWriter
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'runs').mkdir()
        self.pdf=self.root/'upload.pdf'
        writer=PdfWriter();writer.add_blank_page(width=600,height=900)
        with self.pdf.open('wb') as handle:writer.write(handle)
        self.provider=Mock();self.provider.base=qp.BASE;self.provider.model=qp.MODEL;self.provider.timeout=45
        self.provider.metadata.return_value={'models':[{'name':qp.MODEL,'digest':'sha256:local'}]}

    def document(self):
        from tests.extraction.test_financial_layout import layout
        return layout('INVOICE',[('Supplier: Acme Trading',20,60,300,80),('Invoice number: X-42',20,90,300,110),
            ('Date: 2020-01-31',20,120,300,140),('Currency: USD',20,150,300,170),
            ('Total amount:',20,200,180,220),('10.00',200,200,300,220),('Total amount:',20,230,180,250),('12.00',200,230,300,250)])

    def parser(self,document):
        def run(input_pdf,output_parent,project_root=None,verbose=False):
            out=Path(output_parent)/'docling-test';out.mkdir(parents=True,exist_ok=True)
            (out/'document.json').write_text(json.dumps(document),encoding='utf-8')
            (out/'run.json').write_text(json.dumps({'status':'success','source':str(input_pdf),
                'source_sha256':hashlib.sha256(Path(input_pdf).read_bytes()).hexdigest()}),encoding='utf-8')
            return out,{'status':'success'}
        return run

    def reply(self,value):
        self.provider.chat.return_value=json.dumps({'done':True,'model':qp.MODEL,'message':{'content':json.dumps({'total_amount':value})}})

    def execute(self,document=None,**kwargs):
        from functools import partial
        import services.extraction.workflow.document_extraction_service as dis
        from services.extraction.workflow.execution import execute_document_extraction
        real=partial(run_attempt,provider_factory=Mock(return_value=self.provider),renderer=self.render)
        with patch.object(dis,'run_attempt',real):
            result=execute_document_extraction('DOC-1',self.pdf,project_root=self.root,
                parser=self.parser(document or self.document()),**kwargs)
        self.assertEqual(result['execution_status'],'EXTRACTED',result)
        self.assertFalse(result['accounting_approval_performed'])
        return json.loads(Path(result['public_result_path']).read_text(encoding='utf-8'))

    def render(self,pdf,target):
        target.write_bytes(b'rendered page');return [100,100]

    enabled=dict(allow_vlm_inference=True,source_access_authorized=True)

    def assert_parser_values_unchanged(self,public):
        self.assertEqual(public['fields']['total_amount']['value'],None)
        self.assertEqual(public['fields']['total_amount']['state'],'ambiguous')
        self.assertEqual({c['value'] for c in public['fields']['total_amount']['candidates']},{'10.00','12.00'})
        self.assertEqual(public['fields']['invoice_number']['value'],'X-42')
        self.assertEqual(public['routing']['decision'],'human_review')
        self.assertTrue(public['review_required'])
        self.assertFalse(public['safety']['ai_candidate_auto_accepted'])

    def test_off_by_default_no_model_is_called(self):
        public=self.execute()
        self.provider.chat.assert_not_called();self.provider.metadata.assert_not_called()
        self.assertEqual(public['status'],'human_review_required')
        self.assertIsNone(public['selective_recovery'])
        self.assertEqual(public['assessment']['recovery_fields'],[])

    def test_permission_without_source_authorization_calls_no_model(self):
        public=self.execute(allow_vlm_inference=True)
        self.provider.chat.assert_not_called()
        self.assertEqual(public['status'],'human_review_required')

    def test_source_supported_candidate_is_offered_never_applied(self):
        self.reply('12.00')
        public=self.execute(**self.enabled)
        self.assert_parser_values_unchanged(public)
        self.assertEqual(public['status'],'candidate_for_review')
        self.assertEqual(public['selective_recovery']['requested_fields'],['total_amount'])
        cell=public['fields']['total_amount']
        self.assertEqual((cell['ai_candidate'],cell['resolution'],cell['confidence']),('12.00','candidate_for_review',None))
        self.assertEqual(cell['ai_candidate_details']['source_check'],'source_role_verified')
        self.assertEqual(cell['ai_candidate_details']['outcome'],'SOURCE_VERIFIED_CANDIDATE')
        self.assertEqual(cell['ai_candidate_details']['model_digest'],'sha256:local')
        self.assertTrue(cell['ai_candidate_details']['evidence'])
        from services.extraction.contracts.accounting_contract import map_m2_result,effective_value
        row=next(f for f in map_m2_result(public,'DOC-1')['fields'] if f['field_key']=='total')
        self.assertIsNone(row['current_value'])
        self.assertIsNone(effective_value({**row,'corrected_by':None,'corrected_at':None}))
        ai=[c for c in row['candidates'] if c.get('source')=='ai']
        self.assertEqual((len(ai),ai[0]['value'],ai[0]['auto_accepted']),(1,'12.00',False))

    def test_hallucinated_value_is_not_published(self):
        self.reply('99.00')
        public=self.execute(**self.enabled)
        self.assert_parser_values_unchanged(public)
        self.assertEqual(public['status'],'unresolved_needs_review')
        cell=public['fields']['total_amount']
        self.assertEqual((cell['ai_candidate'],cell['ai_candidate_details'],cell['resolution']),(None,None,'unresolved_missing'))
        self.assertNotIn('99.00',json.dumps(public['fields']))

    def test_model_null_stays_unresolved(self):
        self.reply(None)
        public=self.execute(**self.enabled)
        self.assert_parser_values_unchanged(public)
        self.assertEqual(public['status'],'unresolved_needs_review')

    def assert_failure_keeps_parser_result(self):
        public=self.execute(**self.enabled)
        self.assert_parser_values_unchanged(public)
        self.assertEqual(public['status'],'ai_attempt_failed_needs_review')
        self.assertIsNone(public['fields']['total_amount']['ai_candidate'])
        return public

    def test_unavailable_server_keeps_parser_result(self):
        self.provider.metadata.side_effect=URLError('connection refused')
        self.assert_failure_keeps_parser_result();self.provider.chat.assert_not_called()

    def test_model_not_pulled_keeps_parser_result_without_download(self):
        self.provider.metadata.return_value={'models':[]}
        self.assert_failure_keeps_parser_result();self.provider.chat.assert_not_called()

    def test_timeout_keeps_parser_result_and_does_not_retry(self):
        self.provider.chat.side_effect=TimeoutError('slow')
        self.assert_failure_keeps_parser_result();self.provider.chat.assert_called_once()

    def test_malformed_json_keeps_parser_result(self):
        for content in ('{not json','{"total_amount":"12.00","extra":"1"}','{"total_amount":12}','[]'):
            with self.subTest(content=content):
                self.provider.chat.return_value=json.dumps({'done':True,'model':qp.MODEL,'message':{'content':content}})
                self.assert_failure_keeps_parser_result()

    def test_reconciliation_failure_keeps_parser_result(self):
        import services.extraction.workflow.document_extraction_service as dis
        self.reply('12.00')
        with patch.object(dis,'reconcile',side_effect=ValueError('source changed')):
            self.assert_failure_keeps_parser_result()

    def test_recovery_runner_failure_keeps_parser_result(self):
        from services.extraction.workflow.execution import execute_document_extraction
        import services.extraction.workflow.document_extraction_service as dis
        with patch.object(dis,'run_attempt',side_effect=RuntimeError('runner broke')):
            result=execute_document_extraction('DOC-1',self.pdf,project_root=self.root,parser=self.parser(self.document()),**self.enabled)
        self.assertEqual(result['execution_status'],'EXTRACTED')
        self.assert_parser_values_unchanged(json.loads(Path(result['public_result_path']).read_text(encoding='utf-8')))

    def test_resolved_parser_values_are_never_sent_for_recovery(self):
        from tests.extraction.test_financial_layout import layout
        doc=layout('INVOICE',[('Supplier: Acme Trading',20,60,300,80),('Invoice number: X-42',20,90,300,110),
            ('Date: 2020-01-31',20,120,300,140),('Currency: USD',20,150,300,170),('Total amount: 10.00',20,200,300,220)])
        public=self.execute(doc,**self.enabled)
        self.provider.chat.assert_not_called()
        self.assertEqual(public['fields']['total_amount']['value'],'10.00')

    def test_currency_is_never_requested_from_the_model(self):
        from tests.extraction.test_financial_layout import layout
        doc=layout('INVOICE',[('Supplier: Acme Trading',20,60,300,80),('Invoice number: X-42',20,90,300,110),
            ('Date: 2020-01-31',20,120,300,140),('Total amount: $ 10.00',20,200,300,220)])
        public=self.execute(doc,**self.enabled)
        self.provider.chat.assert_not_called()
        self.assertEqual(public['fields']['currency']['state'],'ambiguous')
        self.assertEqual(public['assessment']['fields']['currency']['reason'],'CURRENCY_SYMBOL_DOES_NOT_IDENTIFY_CURRENCY')


class AssessmentTests(unittest.TestCase):
    def assess(self,document):
        from services.extraction.parsing.map_invoice import map_document
        from services.extraction.parsing.extraction_v1 import adapt_result
        from services.extraction.classification.route_extraction import assess_extraction
        mapped=map_document(document);mapped['run_id']='assess'
        payload=adapt_result(mapped,'DOC','mapped.json','a'*64).model_dump()
        payload.update(schema_version='1.1.0-proposed',
            document_type={**classify_document(document),'source_document_json':'d.json','source_json_sha256':'a'*64},
            classification_attachment={'parent_extraction_run_id':'p','extraction_result_path':'e','extraction_result_sha256':'a'*64,
                                       'classification_result_path':'c','classification_result_sha256':'a'*64})
        return assess_extraction(payload)

    def rows(self,*cells):
        from tests.extraction.test_financial_layout import layout
        return layout('INVOICE',[(t,20,60+i*30,400,80+i*30) for i,t in enumerate(cells)])

    def test_complete_required_fields_are_sufficient_for_validation_only(self):
        result=self.assess(self.rows('Supplier: Acme','Date: 2020-01-31','Currency: USD','Total amount: 10.00'))
        self.assertEqual(result['decision'],'PARSER_SUFFICIENT_FOR_VALIDATION')
        self.assertTrue(result['review_required']);self.assertNotIn('confidence',json.dumps(result))

    def test_unresolved_required_field_goes_to_review_with_a_reason(self):
        result=self.assess(self.rows('Supplier: Acme','Date: 2020-01-31','Total amount: 10.00'))
        self.assertEqual(result['decision'],'HUMAN_REVIEW_REQUIRED')
        self.assertEqual(result['fields']['currency']['reason'],'NO_PRINTED_CURRENCY')

    def test_ambiguous_date_is_for_a_person_not_a_model(self):
        result=self.assess(self.rows('Supplier: Acme','Date: 04/01/2017','Currency: USD','Total amount: 10.00'))
        self.assertEqual(result['fields']['invoice_date']['reason'],'DATE_ORDER_NOT_RECOVERABLE_BY_MODEL')
        self.assertEqual(result['recovery_fields'],[])

    def test_unknown_document_is_never_recovery_eligible(self):
        from tests.extraction.test_financial_layout import layout
        result=self.assess(layout('Notes',[('Some text',20,60,200,80)]))
        self.assertEqual((result['decision'],result['recovery_fields']),('HUMAN_REVIEW_REQUIRED',[]))


class WorkerFlagTests(unittest.TestCase):
    """The worker call-site only; claim fencing is covered by the queue tests."""
    def setUp(self):
        # A developer .env (loaded by database.connection) may set QWEN_RECOVERY_MODE, which
        # takes precedence over ENABLE_QWEN_RECOVERY; isolate both so these tests see only their own flag.
        saved={k:os.environ.pop(k) for k in ('QWEN_RECOVERY_MODE','ENABLE_QWEN_RECOVERY') if k in os.environ}
        self.addCleanup(os.environ.update,saved)

    def run_job(self,env):
        from services.queue import jobs
        from services.queue import attempts
        calls={}
        def execute(**kwargs):
            calls.update(kwargs)
            return {'execution_status':'EXTRACTED','service_run_id':'r','public_result_path':'p'}
        row={'id':'00000000-0000-0000-0000-000000000002','document_id':'00000000-0000-0000-0000-000000000001',
             'tenant_id':'00000000-0000-0000-0000-000000000003','dispatch_token':'token'}
        db=MagicMock();db.__enter__.return_value=db;db.execute.return_value.fetchone.return_value=row
        with TemporaryDirectory() as folder, patch.dict(os.environ,env,clear=False):
            source=Path(folder)/'doc.pdf';source.write_bytes(b'%PDF-')
            with patch('rq.get_current_job',return_value=Mock(id='token')),patch.object(attempts,'connect',return_value=db),\
                 patch.object(attempts,'claim_delivery',return_value=Mock()),\
                 patch('services.extraction.workflow.execution.execute_document_extraction',execute),\
                 patch('services.extraction.persistence.persistence.persist_extraction_result',return_value={}),\
                 patch.object(jobs,'_TEST_DOCUMENT_RESOLVER',lambda _:source):
                jobs.process_document_extraction_job(row['document_id'])
        return calls

    def test_worker_keeps_qwen_off_unless_explicitly_enabled(self):
        os.environ.pop('ENABLE_QWEN_RECOVERY',None)
        calls=self.run_job({})
        self.assertIs(calls['allow_vlm_inference'],False);self.assertIs(calls['source_access_authorized'],False)
        for value in ('false','0','','yes'):
            with self.subTest(value=value):
                calls=self.run_job({'ENABLE_QWEN_RECOVERY':value})
                self.assertIs(calls['allow_vlm_inference'],False)
        os.environ.pop('ENABLE_QWEN_RECOVERY',None)

    def test_explicit_flag_enables_only_with_the_resolved_document(self):
        calls=self.run_job({'ENABLE_QWEN_RECOVERY':'true'})
        os.environ.pop('ENABLE_QWEN_RECOVERY',None)
        self.assertIs(calls['allow_vlm_inference'],True);self.assertIs(calls['source_access_authorized'],True)


if __name__ == '__main__':
    unittest.main()




class ModelDateAndTimeNotationTests(unittest.TestCase):
    """A correct reading must not be lost to its notation, and a date order is never guessed."""

    def normalize(self, name, value):
        from services.extraction.ai.qwen_provider import normalize_model_value
        return normalize_model_value(name, value)

    def test_unambiguous_printed_date_is_converted(self):
        self.assertEqual(self.normalize('invoice_date', '29/12/2023'), '2023-12-29')
        self.assertEqual(self.normalize('transaction_date', '29/01/2018 4:40:40 PM'), '2018-01-29')
        self.assertEqual(self.normalize('due_date', '2018-01-29T16:40:40'), '2018-01-29')

    def test_either_way_date_is_left_to_be_rejected(self):
        self.assertEqual(self.normalize('invoice_date', '04/01/2017'), '04/01/2017')
        self.assertEqual(self.normalize('invoice_date', '31/02/2023'), '31/02/2023')

    def test_time_is_taken_from_a_whole_printed_timestamp(self):
        self.assertEqual(self.normalize('transaction_time', '29/01/2018 4:40:40 PM'), '16:40:40')
        self.assertEqual(self.normalize('transaction_time', '2018-01-29T16:40:40'), '16:40:40')

    def test_other_fields_are_untouched(self):
        self.assertEqual(self.normalize('receipt_number', '29/12/2023'), '29/12/2023')


class VisualInputTests(unittest.TestCase):
    """What the model is actually sent, and why a candidate was held back."""

    def test_a_large_page_is_bounded_and_a_small_page_is_not_enlarged_past_its_scan(self):
        import pypdfium2 as pdfium, tempfile
        from pathlib import Path
        from services.extraction.ai.run_selective_qwen import render_single_page
        from PIL import Image
        for size, expected in (((595, 842), (919, 1300)), ((298, 422), (621, 879))):
            pdf = pdfium.PdfDocument.new()
            pdf.new_page(*size)
            import io
            stream = io.BytesIO(); pdf.save(stream)
            with tempfile.TemporaryDirectory() as folder:
                dimensions = render_single_page(stream.getvalue(), Path(folder) / 'page.png')
                with self.subTest(size=size):
                    self.assertLessEqual(max(dimensions), 1300)
                    self.assertAlmostEqual(dimensions[0], expected[0], delta=2)
                    self.assertAlmostEqual(dimensions[1], expected[1], delta=2)
                    self.assertEqual(list(Image.open(Path(folder) / 'page.png').size), dimensions)

    def test_every_request_carries_one_real_image(self):
        import base64
        payload = qp.prepare_request(b'PNG-BYTES', ['receipt_number'], profile='receipt', source_context={'page': 1})
        self.assertEqual(base64.b64decode(payload['messages'][0]['images'][0]), b'PNG-BYTES')
        with self.assertRaises(ValueError):
            qp.prepare_request(b'', ['receipt_number'], profile='receipt')


class HeldBackCandidateWordingTests(unittest.TestCase):
    """The review page must say why a real candidate was not applied, not that nothing was found."""

    @staticmethod
    def guidance(check, key='reference'):
        from services.extraction.contracts.reliability import field_guidance
        return field_guidance({'field_key': key, 'extraction_state': 'missing', 'evidence': [],
                               'candidates': [{'source': 'ai', 'value': 'BTG-052332', 'source_check': check}]})

    def test_role_not_verified_is_stated(self):
        result = self.guidance('source_text_match')
        self.assertEqual(result['evidenceStatus'], 'model_suggested')
        self.assertEqual([w['code'] for w in result['warnings']], ['MODEL_CANDIDATE_ROLE_NOT_VERIFIED'])
        self.assertIn('BTG-052332', result['warnings'][0]['message'])
        self.assertNotIn('No supported source value', result['warnings'][0]['message'])

    def test_role_verified_is_a_different_state(self):
        result = self.guidance('source_role_verified')
        self.assertEqual(result['evidenceStatus'], 'model_source_verified')
        self.assertEqual([w['code'] for w in result['warnings']], ['MODEL_CANDIDATE_ROLE_VERIFIED'])

    def test_no_candidate_keeps_the_plain_message(self):
        from services.extraction.contracts.reliability import field_guidance
        result = field_guidance({'field_key': 'reference', 'extraction_state': 'missing', 'evidence': [], 'candidates': []})
        self.assertEqual(result['evidenceStatus'], 'missing')
        self.assertIn('No supported source value', result['warnings'][0]['message'])

    def test_no_confidence_is_invented(self):
        self.assertNotIn('confidence', self.guidance('source_role_verified'))

    def test_absent_tax_says_none_was_assumed(self):
        from services.extraction.contracts.reliability import field_guidance
        result = field_guidance({'field_key': 'tax', 'extraction_state': 'missing', 'evidence': [], 'candidates': []})
        self.assertIn('none was assumed', result['warnings'][0]['message'])

    def test_mixed_heading_and_payment_wording_are_explained_without_claiming_settlement(self):
        from services.extraction.contracts.reliability import extraction_guidance
        warnings = extraction_guidance({'document_type': {'value': 'invoice', 'reason': 'EXPLICIT_DOCUMENT_HEADING',
            'candidates': [{'value': 'invoice', 'evidence': {'text': 'Invoice'}}],
            'signals': [{'kind': 'copy_marker', 'text': '-- Reprint Receipt --'}, {'kind': 'payment_cue', 'text': 'Cash'}]}}, [{'x': 1}])
        self.assertEqual({w['code'] for w in warnings}, {'MIXED_DOCUMENT_HEADINGS', 'PAYMENT_WORDING_PRINTED'})
        self.assertIn('settlement', next(w for w in warnings if w['code'] == 'PAYMENT_WORDING_PRINTED')['message'])


class ImageReadingAgainstOcrTests(unittest.TestCase):
    """A name the model read correctly from the image, where OCR misread one character."""

    def check(self, field, value, *lines):
        from services.extraction.classification.reconcile_selective import check_candidate_against_source
        entries = [{'self_ref': f'#/m2_text_lines/{i}', 'text': text, 'label': 'text',
                    'prov': [{'page_no': 1, 'bbox': {'l': 10, 't': 10 + 20 * i, 'r': 200, 'b': 25 + 20 * i, 'coord_origin': 'TOPLEFT'}}]}
                   for i, text in enumerate(lines)]
        return check_candidate_against_source(field, value, {'schema_name': 'DoclingDocument', 'pages': {'1': {'size': {'width': 300, 'height': 500}}},
                                                             'texts': [], 'tables': [], 'm2_text_lines': entries})

    def test_close_name_is_offered_for_a_person_and_never_verified(self):
        from services.extraction.classification.reconcile_selective import arbitrate
        result = self.check('merchant', 'TEO HENG STATIONERY & BOOKS', 'TEO HENG STATIONERY G BOOKS', 'Tel 123')
        self.assertEqual(result['status'], 'source_near_match')
        self.assertEqual(result['evidence'][0]['text'], 'TEO HENG STATIONERY G BOOKS')
        self.assertEqual(arbitrate(None, 'TEO HENG STATIONERY & BOOKS', result['status']), 'UNVERIFIED_MODEL_SUGGESTION')

    def test_a_different_name_is_still_rejected(self):
        self.assertEqual(self.check('merchant', 'ACME TRADING', 'TEO HENG STATIONERY G BOOKS')['status'], 'not_found_in_source')

    def test_identifiers_and_amounts_are_never_near_matched(self):
        self.assertEqual(self.check('receipt_number', 'BTG-052333', 'No.:BTG-052332')['status'], 'not_found_in_source')
        self.assertEqual(self.check('total_amount', '6.36', 'Total 6.35')['status'], 'not_found_in_source')
