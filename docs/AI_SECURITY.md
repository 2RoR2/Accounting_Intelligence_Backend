# AccordAI AI security controls

The following controls are the implementation baseline for document extraction.
Uploaded files and OCR are untrusted data. AI output is a candidate only; an
authorised human must approve accounting records before posting.

| # | Control | Status | Implementation / verification |
|---:|---|---|---|
| 1 | Prompt-injection resistance | Implemented | `services/extraction/lite/structure.py`; untrusted delimiters and system rule; adversarial test |
| 2 | OCR input sanitisation | Implemented | `services/extraction/lite/sanitise.py`; `AI_INPUT_SANITISED` count-only issue |
| 3 | Strict output schema | Implemented | `security/ai_guard.py`; allow-list, `{value, raw}`, 500-char fields, 200 lines |
| 4 | Schema failure handling | Implemented | `AI_SCHEMA_INVALID` becomes review-required fallback, not a crash |
| 5 | Evidence grounding | Implemented | Local OCR evidence is required before a value is used |
| 6 | Confidence and suspicious-source review | Implemented | Source confidence and injection findings suppress verified prefill |
| 7 | Sensitive-data masking | Implemented | `services/extraction/lite/privacy.py` masks identifiers before AI calls |
| 8 | External-provider privacy boundary | Implemented | Provider receives masked text only; image sending is disabled for this flow |
| 9 | Supplier-memory poisoning prevention | Implemented | Learning is based on verified human corrections and can be revoked |
| 10 | Tenant isolation | Implemented | Tenant-scoped document resolution, audit queries, and daily AI quota keys |
| 11 | API-key protection | Implemented | Provider keys are read from environment/secrets, never returned in responses |
| 12 | AI rate and cost limits | Implemented | `AI_DAILY_LIMIT` defaults to 500 per company/day; `0` disables |
| 13 | Upload abuse limits | Implemented | `UPLOAD_RATE_LIMIT` defaults to 60 per user/10 minutes; `0` disables |
| 14 | Secure response validation | Implemented | JSON-only provider boundary plus application contract validation |
| 15 | Hallucination prevention | Implemented | Printed-source matching, no inference/calculation, explicit discarded-value outcomes |
| 16 | Explainability | Implemented | Evidence, method, confidence, reason codes, and safe audit metadata are retained |
| 17 | AI auditability | Implemented | Admin-only `/api/ai-audit` and System/Audit read-only table; no prompt/text exposure |
| 18 | Adversarial testing | Implemented | Six synthetic fixtures in `tests/security/test_adversarial_ai.py` |
| 19 | Human approval gate | Implemented | Extraction remains review-required; AI cannot post journals or payments |
| 20 | Security headers and deployment hardening | Implemented | Frame, MIME, referrer, permissions headers; CSP is report-only while frontend compatibility is verified |

## Operational settings

`AI_INJECTION_GUARD` defaults to `on`. `AI_DAILY_LIMIT` defaults to `500` per
tenant per day. `UPLOAD_RATE_LIMIT` defaults to `60` per authenticated user per
10-minute window. Set either limit to `0` only when an operator intentionally
disables that safeguard.

## Reference frameworks

The controls map to the OWASP Top 10 for LLM Applications (2025), especially
prompt injection, sensitive information disclosure, data/model poisoning,
improper output handling, excessive agency, and unbounded consumption. The
governance lifecycle follows the NIST AI Risk Management Framework: govern,
map, measure, and manage, with evidence and human review retained for each
high-impact decision.
