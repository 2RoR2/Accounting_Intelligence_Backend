"""Conservative utility-bill fast path; scans and mixed image documents keep OCR."""
import re


def assess_native_utility_pages(pages):
    if not pages or len(pages)>10:
        return False
    if any(p['characters']<500 or p['largest_image_fraction']>=.25 for p in pages):
        return False
    text='\n'.join(p['text'] for p in pages)
    cues=[r'\bbil\s+elektrik\b|\belectricity\s+bill\b|\butility\s+bill\b',
          r'\bno\.?\s*akaun\b|\baccount\s*(?:no\.?|number)\b',
          r'\bamount\s+due\b|\btotal\s+due\b|\bjumlah\s+bil\s+anda\b|\bjumlah\s+perlu\s+dibayar\b',
          r'\btarikh\s+bil\b|\bbill\s+date\b',r'\bkwh\b']
    return all(re.search(cue,text,re.I) for cue in cues)


def native_utility_fast_path(path):
    import pypdfium2 as pdfium
    try:
        pages=[]
        with pdfium.PdfDocument(path) as pdf:
            if not 0<len(pdf)<=10:return False
            for page in pdf:
                try:
                    width,height=page.get_size()
                    textpage=page.get_textpage()
                    try:text=textpage.get_text_range()
                    finally:textpage.close()
                    area=width*height
                    fractions=[]
                    for obj in page.get_objects():
                        if obj.type==3:
                            left,bottom,right,top=obj.get_bounds()
                            fractions.append(max(0,right-left)*max(0,top-bottom)/area)
                    pages.append({'text':text,'characters':len(text.strip()),'largest_image_fraction':max(fractions,default=0)})
                finally:page.close()
        return assess_native_utility_pages(pages)
    except Exception:
        # Failure to prove eligibility preserves the full parser/OCR behavior.
        return False
