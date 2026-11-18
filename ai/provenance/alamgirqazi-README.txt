---
license: cc-by-4.0
---
# InvoiceOCR-Synth

An annotation-noise-free synthetic dataset of receipt and invoice images for evaluating document information extraction systems, including vision–language models (VLMs) and OCR pipelines.

- **DOI:** [10.57967/hf/9733](https://doi.org/10.57967/hf/9733)
- **Code:** [github.com/alamgirqazi/synthetic-invoice-gen](https://github.com/alamgirqazi/synthetic-invoice-gen)
- **Preprint:** [https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7329421](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7329421)

## Why this dataset

Public receipt and invoice benchmarks rely on human annotation of pre-existing images. When an annotator misreads a faded digit, the ground truth itself violates arithmetic identities (e.g. `amount = quantity × unit_price`), and a model that extracts the correct value is penalised against a noisy label.

This corpus inverts that pipeline: structured JSON records are sampled **first**, with arithmetic identities enforced at generation time, and document images are rendered **from** the records. Because the record is both the data source and the label, ground truth is correct by construction. Image degradation is applied after rendering as a controlled, independent variable, so robustness measurements are not contaminated by label noise.

## What's inside

| Property | Value |
| --- | --- |
| Base documents | 938 |
| Image-quality conditions | 3 (`clean`, `faded`, `bad_scan`) |
| Total evaluation images | 2,814 |
| Schema fields per document | 32 |
| Visual templates | 6 (Jinja2 HTML/CSS layout families) |
| Font families | 19 |
| Currencies | 5 (EUR, GBP, USD, CAD, AUD) |
| Product categories | 14 |
| Avg. line items per document | 3.75 (range 1–10) |
| Avg. tax entries per document | 1.11 (24.6% of documents carry none) |
| Documents with free-text notes | 21.7% |
| Arithmetic consistency of labels | 100%, verified by the released audit script |

Each of the 938 base documents appears under all three degradation conditions with **identical annotations** — the `quality` column identifies the condition, and rows sharing a `file_name` are the same base document. Any accuracy difference between conditions is therefore attributable entirely to model robustness.

### Template distribution

| Template | Documents | | Currency | Documents |
| --- | ---: | --- | --- | ---: |
| `formal_corporate` | 189 | | EUR | 408 |
| `pos_thermal` | 187 | | GBP | 231 |
| `utility_bill` | 177 | | USD | 202 |
| `event_ticket` | 132 | | CAD | 51 |
| `modern_minimal` | 130 | | AUD | 45 |
| `freelancer_invoice` | 123 | | `$` (unresolved) | 1 |

One record carries the literal symbol `$` in `document_currency` instead of an ISO 4217 code. It is otherwise arithmetically consistent and is retained as released; code that partitions the corpus by currency should expect this value.

Two rendering rules are verifiable from the released metadata: thermal receipts always use a monospace font, and serif faces appear in roughly 15% of non-thermal renders.

## Data fields

Each row pairs one document image with its full ground-truth annotation:

- **Supplier identity** — `supplier_name`, `supplier_address`, `supplier_phone`, `supplier_email`, `supplier_website`, `supplier_company_registrations`
- **Customer identity** — `customer_name`, `customer_id`, `customer_address`, `billing_address`, `shipping_address`, `customer_company_registrations`
- **Document references** — `document_type_extended`, `document_number`, `invoice_number`, `receipt_number`, `reference_numbers`, `po_number`
- **Dates and time** — `document_date`, `document_time`, `due_date`, `payment_date`
- **Monetary totals** — `document_currency`, `document_total_amount`, `document_total_net`, `document_total_tax`
- **Classification** — `document_category`, `subcategory`, `locale`
- **Line items** — array of `{description, quantity, unit_price, amount, tax_rate, tax_amount}`
- **Taxes** — array of `{base, rate, code, value}`, grouping line items by tax rate
- **Notes** — free text where the document carries it
- **Provenance** — `_template_used`, `_font_info`
- **Row metadata** — `file_name` (links the three quality variants of a base document), `quality`, `image`

Fields not applicable to a document type are `null` by design — field sparsity is modelled per document type, so POS receipts rarely carry customer addresses while formal invoices carry invoice numbers and due dates. Nulls are part of the ground truth, not missing data.

Two fields are `null` in every record: `shipping_address` and `document_type_extended`. They are retained in the schema because the schema defines the extraction target, and a consistently absent field is legitimate signal for an extraction system.

### Rounding convention

Per-line tax amounts are rounded to the minor currency unit, and each tax-summary `value` is the sum of those rounded amounts. A summary value may therefore differ from an exact percentage of its `base` by up to one minor unit — the same behaviour as real accounting documents, where line-level rounding precedes summation.

## Usage

```python
from datasets import load_dataset

ds = load_dataset("alamgirqazi/invoice-ocr-synthetic", split="train")

# evaluate on one condition
clean = ds.filter(lambda r: r["quality"] == "clean")

# the three variants of one base document share a file_name

# per-template breakdown
from collections import Counter
Counter(r["_template_used"] for r in clean)
```

## How it was made

Three-stage *generate-then-render* pipeline:

1. **Record generation** — `openai/gpt-oss-20b` (temperature 0.7, top-p 0.95) samples structured JSON records under the extraction schema. Arithmetic identities (`amount = quantity × unit_price`; `gross = net + tax`) are stated in the prompt and enforced by a post-generation validation pass that recomputes values upward from quantities and unit prices. All content is instructed to be fictional.
2. **Rendering** — each validated record is rendered through one of six Jinja2 HTML/CSS templates with randomised fonts and colour palettes, converted to PDF with WeasyPrint, and rasterised to PNG at 150 DPI with PyMuPDF.
3. **Degradation** — each render is reproduced under three conditions. **`clean`** applies no transformation at all: it is the 150 DPI render written directly to PNG, and is the reference against which the other two are read. **`faded`** applies a warm yellowing tint with reduced contrast and brightness, a patchy edge-biased vignette, print-head streaks and friction scratches, then an uneven lighting gradient, light sensor noise and mild JPEG compression. **`bad_scan`** tilts the page and exposes the dark scanner-lid border, applies a stronger lighting gradient, then runs an [Augraphy](https://github.com/sparkfish/augraphy) pipeline (ink bleed, low-ink lines, paper tinting and texturing, dirty-roller scan lines, page folding) followed by sensor noise and heavier JPEG compression. Augraphy is used in the `bad_scan` pipeline only; exact stage parameters are in `PRESETS` in `augmentation.py`.

### Validation and exclusion

The validation pass works upward from the line items, so every identity derivable that way holds exactly. The document-level `taxes` summary is not derivable upward — the model emits it as an independent structure — and was consequently left unenforced during generation.

Auditing the completed corpus found 57 of 1,000 records (5.7%) whose tax summary disagreed with their own line items, plus 5 records containing no line items. Because six templates print the tax summary on the page, repairing those records after rendering would have desynchronised the label from the image. All 62 were therefore **excluded rather than repaired**, leaving 938 documents in which every label matches its image exactly.

Excluded file names and reasons are distributed as `excluded_records.txt`, and the audit script that detects them is in the [code repository](https://github.com/alamgirqazi/synthetic-invoice-gen).

## File organisation

The repository contains four files:

| Path | Contents |
| --- | --- |
| `data/train-00000-of-00002.parquet`<br>`data/train-00001-of-00002.parquet` | 2,814 rows, one per image. Each row carries the image bytes, a `quality` column naming the condition, all 32 annotation fields, and the provenance columns `_template_used` and `_font_info`. |
| `ground_truth_released.json` | The 938 label records as a single JSON array, without images. For users who want the labels alone — to score an existing prediction file, or to inspect the schema. |
| `excluded_records.txt` | The 62 records removed by the consistency audit, one per line, as `file_name<TAB>reason`. Provided for transparency; these records are not part of the dataset. |

The images are distributed **inside the Parquet shards**. There is no separate directory of PNG files, so the Parquet is the only source of image data.

### How rows relate to documents

Each base document appears as three rows — one per condition — that share a `file_name` and carry byte-identical annotations. The `file_name` follows the pattern `synth_{index}_{hash}.png`. The `{index}` component is a per-run counter and is **not unique**: the corpus was assembled from two generation runs whose counters both begin at zero, so 175 index values are each shared by two documents. The 8-character MD5 digest disambiguates them, so it is the full `synth_{index}_{hash}` string that identifies a document, and `(file_name, quality)` that identifies a row.

## Recommended workflow

Load the Parquet through `datasets` and filter on `quality`. Everything needed for evaluation is in one place, and no join between files is required.

```python
from datasets import load_dataset

ds = load_dataset("alamgirqazi/invoice-ocr-synthetic", split="train")

clean    = ds.filter(lambda r: r["quality"] == "clean")      # 938 rows
faded    = ds.filter(lambda r: r["quality"] == "faded")      # 938 rows
bad_scan = ds.filter(lambda r: r["quality"] == "bad_scan")   # 938 rows

row = clean[0]
row["image"]                   # PIL.Image
row["supplier_name"]           # ground truth for that image
row["line_items"]              # [{description, quantity, unit_price, amount, tax_rate, tax_amount}, ...]
row["taxes"]                   # [{base, rate, code, value}, ...]
```

To run one model across all three conditions and read the difference as a degradation effect, score each subset separately and align on `file_name`:

```python
for condition in ("clean", "faded", "bad_scan"):
    subset = ds.filter(lambda r: r["quality"] == condition)
    preds  = {r["file_name"]: my_model(r["image"]) for r in subset}
    gold   = {r["file_name"]: r for r in subset}
    # labels are identical across conditions, so any score difference is a robustness effect
```

If you already have predictions keyed by `file_name` and only need the labels, use `ground_truth_released.json` and skip the Parquet download entirely.

## Reproducing the corpus

The full pipeline is released at [github.com/alamgirqazi/synthetic-invoice-gen](https://github.com/alamgirqazi/synthetic-invoice-gen) under the MIT Licence. The prompt template is built by `build_prompt()` in `generate_synthetic_data.py`; degradation stage parameters are in `PRESETS` in `augmentation.py`; and `audit.py` is the script that produced `excluded_records.txt`. Reproduction commands, including the non-default parameters used for this release, are documented in that repository's README.

## Intended uses

- Benchmarking and comparing OCR pipelines, layout-aware transformers, and VLMs on structured extraction
- Controlled robustness studies across image-quality conditions without label-noise confounds
- Fine-tuning and distillation experiments where annotation noise would otherwise bound the accuracy ceiling
- Developing and calibrating evaluation metrics for structured document extraction

## Limitations

**Synthetic content.** Content distributions reflect the inductive biases of the generating model rather than any real commercial population, and layout diversity is bounded by the six templates. Absolute accuracy figures obtained here may not transfer to real-world receipt collections.

**Repetition in structured identifier fields.** Supplier names (86.7% unique) and addresses (94.9%) are diverse, but registration numbers and phone numbers are not: `supplier_company_registrations` is 19.1% unique, with a single value appearing on 258 documents, and `supplier_phone` is 29.2% unique. Per-field accuracy on those two fields should be interpreted with caution, since a model can score well on them without reading the image.

**Category imbalance.** Consulting-type documents account for 32.5% of the corpus.

**Language and region.** All textual content is English. Regional variation is present in currency, address format, and tax rate, but the region is not recorded as an annotation field, so the corpus does not support stratified per-region evaluation.

**Degradation coverage.** The three conditions are parameterised presets and do not span all real capture conditions — extreme perspective, handwriting, and crumpled paper are absent. All documents are single-page.

**No human realism assessment.** Visual realism of the rendered documents has not been evaluated by human raters.

## Ethics and privacy

No real documents were collected. All names, addresses, registration numbers, and bank details are machine-generated under an instruction to produce fictional content, and the corpus carries no privacy or confidentiality constraints. Note that generated registration numbers follow national formats and are not guaranteed to be unallocated.

## Citation

```bibtex
@misc{qazi2026invoiceocrsynth,
  author    = {Qazi, Alamgir Munir and Nasir, Jamal Abdul and
               Chambeth, Pratheesh and Qureshi, Waqar Shahid},
  title     = {InvoiceOCR-Synth: An annotation-noise-free synthetic dataset of
               receipt and invoice images for document information extraction},
  year      = {2026},
  publisher = {Hugging Face},
  url       = {https://huggingface.co/datasets/alamgirqazi/invoice-ocr-synthetic},
  doi       = {10.57967/hf/9733}
}
```

## Acknowledgements

This work is partially supported by the European Union's Horizon Europe programme under grant agreement No 101135757, project [AI4Debunk](https://ai4debunk.eu).

## Licence

Data released under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Generation and rendering code released under the MIT Licence.