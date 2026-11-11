from copy import deepcopy
from pathlib import Path
from services.extraction.parsing.native_pdf_policy import assess_native_utility_pages,native_utility_fast_path


def pages():
    return [{'characters':1200,'largest_image_fraction':.14,
             'text':'Electricity Bill\nAccount Number\nBill Date\nAmount Due\nkWh'}]


def test_native_utility_content_can_use_fast_path():
    assert assess_native_utility_pages(pages())


def test_scans_large_image_regions_sparse_pages_and_non_bills_keep_ocr():
    for field,value in [('characters',80),('largest_image_fraction',.8),('text','INVOICE\nAmount Due')]:
        candidate=pages();candidate[0][field]=value
        assert not assess_native_utility_pages(candidate)
    mixed=pages()+[{'characters':0,'largest_image_fraction':1,'text':''}]
    assert not assess_native_utility_pages(mixed)


def test_metadata_or_filename_does_not_enable_fast_path(tmp_path):
    path=tmp_path/'Electricity-Bill-Account-Number-Amount-Due.pdf'
    path.write_bytes(b'not a readable PDF')
    assert not native_utility_fast_path(path)
