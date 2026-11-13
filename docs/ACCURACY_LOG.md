# Accuracy log (task MG1 / P1.2)

One change at a time, each measured on the synthetic **tuning** set (60 documents,
`ai/synthetic/output/tuning`, generator `ai-synthetic-my/1.0`) with the same settings.
The holdout set is never used here.

Settings for every row unless noted: `AI_EXTRACTION=text`, `AI_PROVIDERS=gemini`
(`gemini-3.5-flash-lite`, OpenAI-compatible endpoint), `AI_PREFILL=verified`, `DATE_ORDER=DMY`,
`LITE_OCR_MAX_SIDE=1280`, `OCR_THREADS=2`, benchmark `--pace 5`.

Command:

```bash
AI_PROVIDERS=gemini AI_BASE_URL= AI_MODEL= AI_API_KEY= AI_EXTRA_BODY= .venv/Scripts/python.exe scripts/lite_benchmark.py --ai text --env-file .env --manifest ai/synthetic/output/tuning --pace 5
```

Accuracy is strict exact match over labelled fields (`ai/evaluation/field_accuracy.py`);
"address-normalised" counts an address as correct when only separators differ.

| # | Change | Before (strict) | After (strict) | After (address-normalised) | Coverage | Report |
|---|---|---|---|---|---|---|
| 0 | Baseline: lite engine as merged in Phase 0 | – | 543/798 = 68.05% | 73.68% | 76.07% | `runs/p12-tuning-gemini-3.5-flash-lite.json` |
| 1 | Field profiles ask for every printed field the contract stores: seller SSM/SST/TIN, buyer address and tax id, bill customer, receipt rounding, note reason and note seller address (`structure.PROFILE_FIELDS`, `MODEL_FIELDS`) | 68.05% | 673/798 = 84.34% | 94.49% | 96.74% | `runs/mg1-c1-profile.json` |
| 2 | Addresses keep the printed line breaks: each part is matched to its OCR row; parts on one row are joined with ", ", rows with a newline (`SourceIndex.address_lines`) | 84.34% | 750/798 = 93.98% | 94.49% | 96.74% | `runs/mg1-c2-address.json` |
| 3 | A bill's printed number is stored as `bill_number` (was `bill_reference`, which the evaluator and standardisation read second); `bill_number` added to the document-name lookup | 708/752 = 94.15% * | 726/752 = 96.54% * | – | 93.23% (run) | `runs/mg1-c3-bill-number.json` |

| 4 | `billing_period`: when two printed dates are joined by a dash/"to"/"hingga", store `<date> - <date>` with both dates exactly as printed (OCR drops the spaces round the dash) | 711/736 = 96.60% ** | 720/736 = 97.83% ** | 96.12% (run) | 97.37% (run) | `runs/mg1-c4-billing-period.json` |

| 5 | Amounts with a decimal comma or spaced thousands (`6 204,19`, `1.234,56`, `6 204.19`) are read correctly; before, `parse_amount('6 204,19')` gave 620419.00 and the source matcher could not see such amounts. Targets the public set; the tuning set prints none | 762/782 = 97.44% *** | 761/782 = 97.31% *** | 97.74% (run) | 99.00% (run) | `runs/mg1-c5-decimal-comma.json` |

| 6 | Address matcher places the longest run of consecutive parts on one row before matching parts one by one (a short part such as "No. 2" no longer lands on another row such as "No. 23") | 205/213 = 96.24% **** | 206/213 = 96.71% **** | – | – | `runs/mg1-c6-address-runs.json` (15 of 60 documents) |

\*\*\*\* Gemini answered `AI_RATE_LIMITED` after 15 documents in run 6 (4 retries; very likely the
free tier's daily request limit after about 450 calls that day), so the run stopped as the rules
require. Compared on the 14 documents runs 5 and 6 both completed.

## Result of this round

Stopped after change 6: the 6-change limit was reached and changes 5 and 6 each gained under 1
point. No Alibaba tokens were used. The holdout set was not run.

**Final tuning accuracy: 776/798 = 97.24% strict exact match** (97.74% address-normalised,
coverage 99.00%), full 60-document run with no provider errors, code as of change 5
(`runs/mg1-c5-decimal-comma.json`). Change 6 is in the code but was measured on 14 documents only.

Config that produced it: `EXTRACTION_ENGINE=lite`, `AI_EXTRACTION=text`, `AI_PROVIDERS=gemini`
(`gemini-3.5-flash-lite`), `AI_PREFILL=verified`, `DATE_ORDER=DMY`, `AI_MAX_RETRIES=4`,
`AI_TIMEOUT_SECONDS=30`, `LITE_OCR_MAX_SIDE=1280`, `OCR_THREADS=2`; model time mean 6.5 s, median
3.7 s, max 34.8 s per document (the long tail is the provider retrying 5xx answers); 2,397 tokens per
document on average.

What is left on the tuning set, by cause (from run 5):

| Cause | Fields | Fixable by |
|---|---|---|
| "PAID / DIBAYAR" stamp drawn over the grand total or amount due | 6 | not fixable from text; correctly left empty, never guessed |
| OCR misreads of note numbers on degraded scans (`N&603-4040`) copied through the source check | 7 | larger OCR size or vision fallback (P1.3) |
| Bill header label runs into its value in the generator (`Pay Befo�8:11-2026`) | 2 | generator layout fix (changes the tuning set) |
| Address parts on rows split by OCR | 6 | partly change 6 |
| Model added a postcode to a TIN | 1 | prompt wording |

Caveat: this is the synthetic tuning set the changes were made against. It says how well the
pipeline reads these layouts, not how it will do on real client documents; the holdout set and the
public 35 are the fairer checks.

\*\*\* Compared on the 59 documents runs 4 and 5 both completed; the −0.13 point difference is
run-to-run model variation (no tuning document uses a decimal comma). Full run 5 had no provider
errors: **776/798 = 97.24%** strict.

\*\* Compared on the 56 documents that runs 2, 3 and 4 all completed (Gemini failed on
`TUN-INVOICE-038` in run 4 even with 4 retries). Full run 4: 763/798 = **95.61%** strict.

\* Gemini returned HTTP errors on 3 invoice documents in run 3 (`AI_HTTP_ERROR` after 2 retries), so
those documents had no fields. Rows 2 and 3 are therefore compared on the 57 documents both runs
completed. The full-run figure for run 3 was 726/798 = 90.98%; with the 3 failed documents scored
as in run 2 it would be 768/798 = 96.2% (a projection, not a measurement). From run 4 on,
`AI_MAX_RETRIES=4`.
