# Architecture and implementation boundaries

Status: agreed direction from our planning; implementation and data contract still require validation.

Rachel M1: upload/job orchestration -> Jonathan M2: extraction -> Aina M3: accounting checks -> Aleeya M4: canonical storage and revisions -> Angeline M5: source/field review and corrections. This describes responsibilities; actual API/control flow must be agreed jointly.

Inside M2:
1. Check file type and supported document class. Preserve document identity.
2. Parse PDF text/layout/tables with Docling. Keep structured document output and available page/bounding-box evidence.
3. Explicit mapping associates document content with header fields and line-item rows. Docling text export is not a completed invoice schema.
4. Route selected unresolved fields to local Qwen2.5-VL-7B through Ollama, supplying the relevant rendered image/crop and context. Preserve parser/model disagreements.
5. Validate response structure and produce proposed values plus review reasons. Unknown is null, not zero. No automatic accounting approval.

Start with one request at a time. Ollama is the initial serving runtime; do not also build vLLM infrastructure now. No external AI fallback is assumed authorised. The lab is a booked experiment/integration environment, not a permanently accessible production server.

Python scripts connect these stages. Dropping a file into data/ does not trigger processing. VS Code is the editor. The SSD stores files; the PC/GPU executes the code.

## Tasks in order
- Agree the contract and evidence coordinate convention with the other module owners.
- Reproduce environment setup and model paths after logout.
- Implement parser conversion; export structured JSON and readable text.
- Implement header/row mapping with missing/ambiguous field handling.
- Implement local Qwen image call with timeout, HTTP error handling and response validation.
- Implement selective routing and fallback to review; do not erase disagreements.
- Integrate through a mock/real adapter selected explicitly by Rachel.
- Evaluate parser-only, direct VLM and selective hybrid on the same labelled cases.

## Evaluation
Report header exact match, missing/unsupported values, line-item alignment, evidence correctness, complete-record correctness, failures and processing time. Separate cold model loading from warm extraction and from full upload-to-review time. Measure human correction time with a stated procedure. Keep failures in workload accounting. Use held-out documents after tuning stops.

95% field accuracy, 70% manual-effort reduction and 20-second processing are project targets, not established results. Start with feasibility; use measured results to decide whether selective VLM assistance earns its extra cost.

## Sprint 1 exit (6 October, supplied schedule)
Minimum: parser/mapping baseline, one independent local Qwen test, proposed contract agreed by teammates, mock success/review/failure cases, setup records and honest initial results. Selective routing, broader integration and evaluation continue after these foundations work.
