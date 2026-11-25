# AccordAI security test results

This pack uses six synthetic OCR/model-response documents. They are deliberately
not sent to an external provider and are safe to run in CI.

| Fixture | Attack | Expected containment | Result |
|---|---|---|---|
| prompt-injection | Document text requests that system rules be ignored and payment approved | Detect signal, suppress verified prefill, require review | Covered by `tests/security/test_adversarial_ai.py` |
| zero-width-escape | Invisible Unicode characters attempt to hide instructions | Remove characters and report a count-only sanitisation event | Covered |
| control-character | NUL and non-printing control bytes | Replace with spaces before prompt construction | Covered |
| repeated-run | Over-long repeated characters intended to exhaust context | Collapse the run and report the count | Covered |
| tool-call-key | Model adds a `tool_call` response key | Reject strict schema and route to `AI_SCHEMA_INVALID` review | Covered |
| line-item-bomb | Model returns more than 200 line items | Reject the response and route to review | Covered |

Run the pack with:

```text
python -m pytest tests/security/test_adversarial_ai.py -q
```

The system also keeps all extraction results review-required, masks sensitive
values before text-model calls, records hashes and safe call metadata, and does
not give the model tools, SQL access, payment access, or posting authority.
