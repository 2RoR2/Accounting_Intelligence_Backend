# M2 unchanged baseline

This is the verified pre-refactor snapshot of the SSD Module 2 workflow. All
32 imported files, including all five original test files (63 tests), retain
their original bytes. `manifest.json` records their relative paths, sizes and
SHA-256 hashes. `.gitattributes` disables line-ending conversion for the snapshot.

The source is `AccountingIntelligence_SSD_Starter/AccountingIntelligence` on
the external SSD. The original `project`, `data` and `lab_ready/samples` layout
is intentional: scripts infer their storage root from their own location, and
the tests import the neighbouring scripts. Keep this snapshot unchanged during
later package refactoring.

## Run the original tests

On this Windows machine, bare `python` resolves to `C:\Python27\python.exe`.
That interpreter cannot install the pinned Pydantic 2 dependency. Use the
verified Python 3.12 runtime explicitly from PowerShell; it already contains
Pydantic 2.13.5, so no installation is needed for this command:

```powershell
$M2Python = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $M2Python -B .\baseline\verify.py
```

If installation is needed, select the same interpreter for pip:

```powershell
& $M2Python -m pip install -r .\baseline\requirements-test.txt
```

For a different machine or a dedicated environment, set `$M2Python` to that
environment's Python 3.12 executable. Do not change the dependency pin to
accommodate Python 2.7.

The verifier checks all imported hashes, requires exactly 63 discovered tests,
and fails on test failures, errors or skips. The suite uses mocked providers;
it needs neither Docling models nor an Ollama server. The existing backend's
`unittest discover -s tests` command does not include this separate baseline;
run the verifier explicitly.

## Verification completed

The port was verified on 2026-10-07 (Asia/Kuala_Lumpur), from backend branch
`module2-baseline`, starting at `22daed2f20cf8bf46cb88efcb9ec49a019193d25`.

| Check | Result |
| --- | --- |
| Imported source, tests, contracts and fixture hashes | 32/32 match the SSD |
| Original component tests | 63 passed, zero skipped |
| Original synthetic invoice, fresh Docling conversion | 12/12 comparisons |
| Changed values with two line items, fresh conversion | 16/16 comparisons |
| Missing invoice number, fresh conversion | 12/12 comparisons; null preserved |
| Fresh parser JSON versus saved parser JSON | Byte-identical for all three PDFs |
| Mapped fields, candidates, evidence, rows and issues | Equal to all three saved mapper results |
| Adapter and combined contract | Replayed with verified local source paths and hashes |
| Complete invoices | Parser-only; no provider or renderer call |
| Missing-number selective attempt | Real PDF rendering, mocked provider; only `invoice_number` requested; null result saved separately |
| Original SSD files inspected around replay | 119 unchanged hashes and modification times, including source, fixtures, reference runs and Docling models |

The Windows unit run used Python 3.12.14 and Pydantic 2.13.5. The fresh parser
replay used the existing WSL Python 3.12.14 environment with Docling 2.132.0,
Docling Core 2.99.0, Pydantic 2.13.5, PyPDFium2 5.13.0, Torch 2.14.1+cpu and
Torchvision 0.29.1+cpu. The historical lab run used Python 3.12.3; the patch
version differs, and the parser outputs still matched byte-for-byte.

The copied parser used its original CPU/four-thread settings, OCR disabled,
table structure enabled, existing offline models and no remote services.
Reference parser/mapper run IDs and portable verification results are recorded
in `verification.json`. Detailed logs, source inventories and the local replay
harness are under ignored `.local/`; generated selective artifacts are under
ignored `runs/`. A local ignored `models/docling` link references the SSD model
directory without copying the weights. Cache and temporary outputs were directed
into `.local/`, and Python bytecode generation was disabled.

## Replaying the parser

The actual parser uses the existing **Ubuntu WSL** environment. Bare Windows
`python` launches Python 2.7 and raises a syntax error on the parser's f-strings.
The Windows `$M2Python` runtime used for unit tests does not contain Docling.

With the SSD connected, run this command directly in **PowerShell** on this PC:

```powershell
wsl -d Ubuntu --exec bash /mnt/c/Users/60103/Documents/GitHub/Accounting_Intelligence_Backend/baseline/parse-samples.sh
```

This launcher selects `$HOME/.venvs/accounting-intelligence/bin/python` inside
WSL and parses all three copied PDFs with the unchanged parser. It keeps cache
and temporary writes under `baseline/.local/` and saves each conversion in a new
`baseline/runs/docling-*` directory. It does not call Qwen, install dependencies,
or alter the original source files. `M2_PYTHON` can override the WSL interpreter
for another prepared environment.

The unchanged parser requires existing models at `baseline/models/docling`;
the ignored local link points to the SSD's model files. Do not source the SSD
session setup scripts for this checkout.

Pass each new `document.json`
explicitly to `project/scripts/map_invoice.py --expected ...`, using the matching
expected-answer path from `verification.json`. Avoid `--latest` for reproducible
checks. The local `.local/replay.py` harness used for this port additionally
checks original SSD hashes and compares the full mapped evidence with saved runs.

## Scope and next checkpoint

This snapshot is a component workflow, not an API integration. Existing backend
application files and tests were left unchanged. The backend authentication and
PostgreSQL suite was not part of this baseline verification. No live Qwen
inference was rerun; selective transport/failure behavior is covered by the
original tests and a mocked response during replay.

The copied Markdown contracts are historical proposals: the adapter emits
`1.0.0-proposed`, and the combined model emits `1.1.0-proposed`. Neither is a
team-approved contract. Classification can identify bill/receipt headings, but
the combined invoice extraction model rejects those types. Reconciliation,
approval, storage, upload orchestration and broader accuracy evaluation remain
outside this baseline.

Machine-specific setup scripts, old setup backups, caches, bytecode, model
weights and raw SSD run folders were not imported into the tracked snapshot.
The two copied diagnostic scripts remain historical tools, not service entry
points. The SSD originals remain the source reference.

The unchanged-copy checkpoint now passes. A later refactor can extract reusable
parser/mapper modules and thin CLI wrappers while retaining this snapshot as
regression evidence. No refactoring, commits or pushes were performed here.
