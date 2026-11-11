"""One real image request, limited to the hash-pinned public TNB publisher sample.

No DB, queue or storage clients. Keys must arrive in the process environment.
The model answer remains an unapproved candidate; printed text is checked independently.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from io import BytesIO
from decimal import Decimal

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ['PYTHON_DOTENV_DISABLED']='1'
from services.extraction.ai.cloud_qwen_provider import AlibabaCloudQwenProvider, CloudProviderError
from services.extraction.ai.qwen_provider import prepare_request, parse_json, validate_response

SOURCE_SHA256='de245f5dc685f43924742a215bd085e7ae6a7876dbeef9f76304298846453d53'
SOURCE_URL='https://www.tnb.com.my/assets/files/Sample_Bill_NonDom_e-Invoicing.pdf'
PRICING_URL='https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen3-7-plus'
# Public real receipt (ICDAR2019 SROIE test row 21, CC-BY-4.0 distribution) and its published labels.
RECEIPT_FILE='SROIE-test-21-X51005433543.jpg'
RECEIPT_SHA256='8c9b04ac23522e14dc531cb67b3800bf16579b5953233a99db16b6d3403e3120'
RECEIPT_URL='https://huggingface.co/datasets/jsdnrs/ICDAR2019-SROIE/viewer/default/test?row=21'
RECEIPT_LABELS={'merchant':'UNIHAKKA INTERNATIONAL SDN BHD','transaction_date':'2018-03-13','total_amount':'8.20',
                'merchant_address':'12, JALAN TAMPOI 7/4,KAWASAN PERINDUSTRIAN TAMPOI,81200 JOHOR BAHRU,JOHOR'}


def receipt_input(manifest_path):
    """Original JPG bytes, unchanged, after provenance and hash checks."""
    manifest_path=Path(manifest_path).resolve()
    entry=next(e for e in json.loads(manifest_path.read_text(encoding='utf-8')) if e.get('filename')==RECEIPT_FILE)
    if entry.get('source_url')!=RECEIPT_URL or entry.get('data_class')!='public_real_receipt':
        raise ValueError('PUBLIC_SOURCE_PROVENANCE_MISMATCH')
    path=(manifest_path.parent/entry['filename']).resolve()
    if path.parent!=manifest_path.parent:raise ValueError('PUBLIC_SOURCE_PATH_MISMATCH')
    raw=path.read_bytes()
    if entry['sha256']!=RECEIPT_SHA256 or hashlib.sha256(raw).hexdigest()!=RECEIPT_SHA256:
        raise ValueError('PUBLIC_SOURCE_HASH_MISMATCH')
    from PIL import Image
    with Image.open(BytesIO(raw)) as image:
        pixels=list(image.size)
    return raw,pixels


def receipt_checks(prediction):
    """Compare candidates with the dataset's published labels; never with this model's own output."""
    from ai.evaluation.address_policy import same_address
    def amount(value):
        try:return Decimal(str(value).replace('$','').replace('RM','').strip())
        except Exception:return None
    return {'merchant':(prediction.get('merchant') or '').strip().upper()==RECEIPT_LABELS['merchant'],
            'transaction_date':prediction.get('transaction_date')==RECEIPT_LABELS['transaction_date'],
            'total_amount':amount(prediction.get('total_amount'))==Decimal(RECEIPT_LABELS['total_amount']),
            'merchant_address_normalized':same_address(RECEIPT_LABELS['merchant_address'],prediction.get('merchant_address'))}


def public_input(manifest_path):
    manifest_path=Path(manifest_path).resolve()
    entries=json.loads(manifest_path.read_text(encoding='utf-8'))
    entry=next(e for e in entries if e.get('filename')=='TNB-public-sample-bill.pdf')
    if entry.get('source_url')!=SOURCE_URL or entry.get('data_class')!='public_publisher_sample':
        raise ValueError('PUBLIC_SOURCE_PROVENANCE_MISMATCH')
    path=(manifest_path.parent/entry['filename']).resolve()
    if path.parent!=manifest_path.parent:raise ValueError('PUBLIC_SOURCE_PATH_MISMATCH')
    raw=path.read_bytes()
    if entry['sha256']!=SOURCE_SHA256 or hashlib.sha256(raw).hexdigest()!=SOURCE_SHA256:
        raise ValueError('PUBLIC_SOURCE_HASH_MISMATCH')
    import pypdfium2 as pdfium
    with pdfium.PdfDocument(raw) as pdf:
        page=pdf[0]
        try:
            textpage=page.get_textpage()
            try:text=textpage.get_text_range()
            finally:textpage.close()
            bitmap=page.render(scale=min(150/72,1300/max(page.get_size())))
            try:
                image=bitmap.to_pil()
                try:
                    stream=BytesIO();image.save(stream,format='PNG');pixels=list(image.size)
                finally:image.close()
            finally:bitmap.close()
        finally:page.close()
    return stream.getvalue(),text,pixels


def run(manifest_path):
    report={'status':'not_started','live_inference_completed':False,'model':'qwen3.7-plus',
        'region':'Singapore','source_url':SOURCE_URL,'source_sha256':SOURCE_SHA256,
        'shared_database_writes':0,'queue_operations':0,'accounting_approved':False,
        'fallback':'Keep deterministic baseline and require review; never replace accepted parser values.'}
    if not (os.getenv('QWEN_CLOUD_API_KEY') or os.getenv('DASHSCOPE_API_KEY')):
        return {**report,'status':'blocked','reason':'API_KEY_MISSING_ACCOUNT_ACTIVATION_REQUIRED'},2
    started=time.perf_counter()
    provider=None
    try:
        provider=AlibabaCloudQwenProvider(max_retries=0,timeout=45)
        if provider.model!='qwen3.7-plus':raise ValueError('EXACT_MODEL_REQUIRED')
        image,text,pixels=public_input(manifest_path)
        fields=['issuer','account_number','bill_date','amount_due']
        payload=prepare_request(image,fields,model=provider.model,profile='bill')
        envelope=parse_json(provider.chat(payload))
        prediction=validate_response(envelope,fields,model=provider.model,profile='bill')
        # Exact literal witnesses; date formatting and issuer roles remain human-review candidates.
        supported={k:(v is None or v in text) for k,v in prediction.items() if k in {'account_number','amount_due'}}
        expected={'account_number':'210013217210','amount_due':'197.35'}
        checks={k:prediction.get(k)==v and supported.get(k,False) for k,v in expected.items()}
        usage={k:envelope.get(v) for k,v in [('input_tokens','prompt_eval_count'),('output_tokens','eval_count')]}
        cost=None
        if all(isinstance(v,int) and not isinstance(v,bool) and v>=0 for v in usage.values()):
            cost=str((Decimal(usage['input_tokens'])*Decimal('0.4')+Decimal(usage['output_tokens'])*Decimal('1.6'))/Decimal(1000000))
        report.update(status='passed' if all(checks.values()) else 'review_required',live_inference_completed=True,
            image_sha256=hashlib.sha256(image).hexdigest(),image_pixels=pixels,rendered_source_page=1,
            provider_call=provider.last_call,usage=usage,estimated_usd_uncached=cost,
            pricing_basis='Singapore International, input <=256k; USD 0.40/M input and 1.60/M output; excludes promotions/cache/tax; verify bill',
            pricing_source=PRICING_URL,pricing_checked='2026-11-10',candidate_fields=prediction,
            literal_source_support=supported,independent_expected_field_checks=checks,
            evidence_limitation='Literal matching verifies printed numbers, not all semantic roles; issuer and date require review.')
        return report,0 if report['status']=='passed' else 3
    except (CloudProviderError,ValueError,KeyError,StopIteration,OSError) as error:
        safe=str(error) if isinstance(error,CloudProviderError) else type(error).__name__
        report.update(status='failed',reason=safe,provider_call=provider.last_call if provider else None)
        return report,1
    finally:
        report['total_seconds']=round(time.perf_counter()-started,3)


def run_receipt(manifest_path):
    report={'status':'not_started','live_inference_completed':False,'model':'qwen3.7-plus','region':'Singapore',
        'sample':'sroie-21-receipt','source_url':RECEIPT_URL,'source_sha256':RECEIPT_SHA256,
        'shared_database_writes':0,'queue_operations':0,'accounting_approved':False,
        'fallback':'Keep deterministic baseline and require review; never replace accepted parser values.'}
    if not (os.getenv('QWEN_CLOUD_API_KEY') or os.getenv('DASHSCOPE_API_KEY')):
        return {**report,'status':'blocked','reason':'API_KEY_MISSING_ACCOUNT_ACTIVATION_REQUIRED'},2
    started=time.perf_counter()
    provider=None
    try:
        provider=AlibabaCloudQwenProvider(max_retries=0,timeout=45)
        if provider.model!='qwen3.7-plus':raise ValueError('EXACT_MODEL_REQUIRED')
        image,pixels=receipt_input(manifest_path)
        fields=['merchant','merchant_address','receipt_number','transaction_date','transaction_time','tax_amount','total_amount']
        envelope=parse_json(provider.chat(prepare_request(image,fields,model=provider.model,profile='receipt')))
        prediction=validate_response(envelope,fields,model=provider.model,profile='receipt')
        checks=receipt_checks(prediction)
        usage={k:envelope.get(v) for k,v in [('input_tokens','prompt_eval_count'),('output_tokens','eval_count')]}
        cost=None
        if all(isinstance(v,int) and not isinstance(v,bool) and v>=0 for v in usage.values()):
            cost=str((Decimal(usage['input_tokens'])*Decimal('0.4')+Decimal(usage['output_tokens'])*Decimal('1.6'))/Decimal(1000000))
        report.update(status='passed' if all(checks.values()) else 'review_required',live_inference_completed=True,
            image_sha256=hashlib.sha256(image).hexdigest(),image_pixels=pixels,thinking_enabled=provider.enable_thinking,
            provider_call=provider.last_call,usage=usage,estimated_usd_uncached=cost,
            pricing_basis='Singapore International, input <=256k; USD 0.40/M input and 1.60/M output; excludes promotions/cache/tax; verify bill',
            pricing_source=PRICING_URL,candidate_fields=prediction,published_label_checks=checks,
            label_source='ICDAR2019 SROIE published entities (company, date, address, total)')
        return report,0 if report['status']=='passed' else 3
    except (CloudProviderError,ValueError,KeyError,StopIteration,OSError) as error:
        safe=str(error) if isinstance(error,CloudProviderError) else type(error).__name__
        report.update(status='failed',reason=safe,provider_call=provider.last_call if provider else None)
        return report,1
    finally:
        report['total_seconds']=round(time.perf_counter()-started,3)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--sample',choices=['tnb-bill','sroie-21-receipt'],default='tnb-bill')
    args=parser.parse_args()
    report,code=(run_receipt if args.sample=='sroie-21-receipt' else run)(args.manifest)
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('status','live_inference_completed','model')}))
    return code


if __name__=='__main__':raise SystemExit(main())
