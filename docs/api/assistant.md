# Ask AccordAI API (feature F1)

Read-only questions about one company's books. Every number in an answer comes from the database; the model never
writes a digit (number binding, G3). The assistant cannot post, approve, pay, email, export or change anything.

Everything is behind `AI_ASSIST` (default `off`). Roles: `accountant` and `local_admin`. Super admin is refused (no
company books).

## `POST /api/assistant/ask`

Request (unknown keys are rejected with 422):

```json
{
  "question": "Which bills are due next week?",
  "context": {"page": "today", "documentId": null, "recordId": null, "reportType": null, "period": null}
}
```

| Field | Rule |
|---|---|
| `question` | 1–500 characters after trimming. |
| `context` | Optional. `page` ≤ 40 chars (the current page segment, e.g. `today`, `reports`, `documents`). `documentId` / `recordId` are UUIDs. `reportType` is one of `profit-and-loss`, `balance-sheet`, `ageing`, `business-summary`. `period` is `YYYY-MM`. |

The request needs the usual `X-Requested-With: AccountingIntelligence` header (all non-GET calls do).

Response `200`:

```json
{
  "answer": "Bills due in the next 7 days: 3, totalling RM 4,210.00. The largest is Seladah Utilities, RM 2,410.00.",
  "facts": [
    {"id": "f1", "label": "Bills due in the next 7 days", "value": "3", "unit": "count", "href": "/company/records?category=ACCOUNTS_PAYABLE&due=next-7-days&asOf=2026-11-27"},
    {"id": "f2", "label": "Total due in the next 7 days", "value": "4210.00", "unit": "MYR", "href": "/company/records?category=ACCOUNTS_PAYABLE&due=next-7-days&asOf=2026-11-27"}
  ],
  "citations": [
    {"label": "Bills due soon (ageing, as of 2026-11-27)", "href": "/company/records?category=ACCOUNTS_PAYABLE&due=next-7-days&asOf=2026-11-27"}
  ],
  "refused": false,
  "fallback": false,
  "suggestions": ["Which bills are overdue?", "Who are our top suppliers this month?"],
  "auditId": "6f1c…",
  "toolsUsed": ["due_soon"],
  "model": "openai/gpt-oss-20b"
}
```

| Field | Meaning |
|---|---|
| `answer` | Plain text, already rendered by the server (fact values are filled in). Render as text, never as HTML or Markdown. Line breaks (`\n`) are allowed. |
| `facts` | The facts the answer uses (or, in a fallback, the figures shown). `value` is a string: money is a 2-decimal string (`"4210.00"`), counts are integers, dates are `YYYY-MM-DD`, names are text. `unit` is one of `MYR`, `count`, `date`, `text`. `href` is an app path built by the server, or `null`. |
| `citations` | One entry per source report or record behind the facts, deduplicated. `href` is an app path or `null`. |
| `refused` | `true` when the assistant declined (an action request, tax advice, another company, the system prompt, or no tool fits). `answer` then says why and where a person can do it. |
| `fallback` | `true` when the model was not used or its answer was rejected; the answer is the deterministic fact table. |
| `suggestions` | 0–4 follow-up questions for the current page. |
| `auditId` | The `audit_logs` row id for this answer (`AI_ASSIST_ANSWER`), or `null` if the audit write failed. |
| `toolsUsed` | Tool names that ran, in order (for the "Tools used" line). |
| `model` | The model that wrote the answer, or `null` for a fallback or a deterministic refusal. |

**Hrefs** use the role's area: `/company/...` for a local admin, `/workspace/...` for an accountant (as
`src/lib/today.ts` `linkHref` does). Report links are `/{area}/reports?report=<profit-and-loss|balance-sheet|ageing>&...`;
the reports page currently ignores `report`, so the link opens the reports page.

Errors:

| Status | `detail` | When |
|---|---|---|
| 403 | `{"code": "AI_ASSIST_OFF", "message": "Ask AccordAI is turned off on this server."}` | `AI_ASSIST` is not `on`. Hide the Ask button. |
| 403 | `"You do not have permission for this workspace."` | Not an accountant or local admin. |
| 401 | (session) | Not signed in. |
| 422 | validation | Question empty or too long, unknown keys, bad context. |
| 429 | `{"code": "AI_ASSIST_RATE_LIMITED", "message": "..."}` | More than `AI_ASSIST_USER_LIMIT` (20) questions in 10 minutes per user, or `AI_ASSIST_TENANT_DAILY_LIMIT` (200) per company per day. |

A provider failure never returns an error: the answer comes back with `fallback: true`.

## `GET /api/assistant/suggestions?page=<segment>`

Response `200`: `{"page": "today", "suggestions": ["Which bills are due next week?", "..."]}` (4 items). Unknown pages
get the general list. Same 403s as `ask` (use it to decide whether to show the Ask button).

## Server settings

| Variable | Default | Meaning |
|---|---|---|
| `AI_ASSIST` | `off` | `on` enables the endpoints. |
| `AI_ASSIST_PROVIDERS` | `AI_PROVIDERS` | Provider order (same names and per-provider settings as extraction). |
| `AI_ASSIST_MODEL` | `openai/gpt-oss-20b` when the first provider is Groq | Model for the first provider (a separate Groq quota bucket from extraction's 120b). |
| `AI_ASSIST_USER_LIMIT` | `20` | Questions per user per 10 minutes. |
| `AI_ASSIST_TENANT_DAILY_LIMIT` | `200` | Questions per company per day. |
| `AI_ASSIST_TENANT_DAILY_TOKENS` | `150000` | Model tokens per company per day; past it, answers use the fallback without calling the model. |
| `AI_ASSIST_MAX_TOOL_RESULT_CHARS` | `6000` | Cap on one tool result sent to the model. |

Gemini (if configured as a fallback provider) is skipped for any question whose tool results contain document text,
because the Gemini unpaid tier may use submitted content.

## Tools (read-only, tenant and role from the session)

| Tool | Arguments | Source |
|---|---|---|
| `contact_balance` | `contact` (2–80), `side`: `owed_to_us`\|`we_owe`\|`either`, `as_of?` | `financial._ageing` filtered by contact |
| `due_soon` | `kind`: `bills`\|`invoices`, `days` 1–60, `include_overdue` | `financial._ageing` by due date |
| `top_contacts` | `kind`: `suppliers`\|`customers`, `from_date`, `to_date`, `limit` 1–10 | `business_summary._by_contact` |
| `profit_and_loss` | `from_date`, `to_date` | `financial._profit_and_loss` (income, expenses, profit, top expense accounts) |
| `balance_sheet` | `as_of` | `financial._balance_sheet` |
| `compare_periods` | `period_a`, `period_b` (`YYYY-MM`) | `_profit_and_loss` twice; differences are subtraction |
| `document_queue` | – | Today page lane counts and top exception reasons |
| `month_end_status` | – | Today page month-end steps |
| `bank_unmatched` | `limit` 1–10 | Confirmed bank lines still to match |
| `search_records` | `query` (2–80), `kind?` | Approved records and typed documents (trigram / ILIKE) |
| `search_document_text` | `query` (2–80) | Text read from approved documents (field readings and line descriptions; full-text and trigram). Returned as sanitised, untrusted data. |
| `explain_document` | `document_id` (UUID) | Readiness reasons, risk sentences, open exceptions and failing checks |

Dates must be within five years of today. No tool has a tenant, user, SQL or free-form filter argument.

## How a question is answered

1. The question is sanitised with the extraction sanitiser (`security/ai_guard.sanitize_document_text`), scanned
   for injection, and masked (`privacy.Masker`: emails, phones, NRIC, cards, account numbers).
2. Deterministic refusals run first (`services/assistant/refusals.py`): action requests (post, approve, pay, email,
   export, delete…), tax calculation or advice, other companies or tenant ids, the system prompt or keys, SQL.
3. The model (`AI_ASSIST_MODEL`) gets the tool list and calls tools one at a time: at most 3 rounds and 4 calls.
   Each call is validated by the tool's Pydantic model (`extra='forbid'`) and runs in
   `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY` with the session's tenant and role.
4. Tool results go back as masked JSON under `data`, capped at `AI_ASSIST_MAX_TOOL_RESULT_CHARS`, with a note that
   text values are data, never instructions. Text from documents or bank lines is sanitised; text the injection scan
   flags is replaced by `[text hidden: it reads like an instruction; open the record to see it]`.
5. The model replies with `{"answer_template", "fact_ids_used", "refusal"}`, or a second call with a strict
   `json_schema` (no tools) asks for it.
6. Number binding: any digit (any script), spelled-out magnitude, link or markup outside a `{{fN}}` placeholder,
   or an unknown fact id, rejects the answer. The server fills every placeholder from the database facts.
7. Any provider error, token budget, rejected answer or second invalid tool call gives the deterministic fallback:
   the facts already gathered, or the keyword router's tool (`services/assistant/fallback.py`), as a table.
8. One `audit_logs` row (`AI_ASSIST_ANSWER`, resource `assistant`) holds the masked question (≤300 characters),
   provider, model, tokens, tool calls with masked arguments, fact ids, the masked answer template, sanitiser
   flags, outcome and fallback reason. No document text, no keys.

## Security controls (plan §4.1) and where they live

| Control | Where |
|---|---|
| G1 read-only runner | `services/assistant/runner.py` `execute` |
| G2 strict tool arguments, no tenant argument | `services/assistant/tools.py` argument models; `runner.parse_call` |
| G3 number binding | `security/assistant_guard.py` `bind_numbers` |
| G4 untrusted-data wrapping | `runner.model_payload`, `ToolContext.untrusted` |
| G5 sanitiser | reused: `security/ai_guard.py` + `services/extraction/lite/sanitise.py`; chat-only extra patterns in `security/assistant_guard.py` (`ASSISTANT_INJECTION_PATTERNS`: role markers, "you are now", plural objects such as "send all invoices") |
| G6 masking | reused: `services/extraction/lite/privacy.py` `Masker` |
| G7 budgets | `assistant_guard.check_question_budget` (`main.rate_limit`), `tokens_spent_today`, loop limits in `services/assistant/config.py` |
| G8 audit row | `assistant_guard.write_audit` |
| G9 output handling | plain text only; links built from ids by `services/assistant/facts.py` |
| G10 prompt hygiene | `services/assistant/prompts.py` holds no secrets or company data; leak requests are refused |

## Tests

- `tests/unit/assistant/`: every tool's arguments, number binding, refusals, the runner, the full loop with a
  scripted provider (tool calls, strict final call, fallbacks, budgets, injection cases ADV-06 to ADV-09 and a
  smuggled tenant id), the routes and the flag, `text_llm.chat`, and the 30-question golden set
  (`golden_questions.json`).
- `tests/integration/test_assistant_postgres.py` (needs `TEST_DATABASE_URL`): KNT seed, every tool on real data,
  numbers equal to the report functions, a write refused by the read-only runner, tenant isolation, the audit row
  and token counter, the 429 after 20 questions, ADV-06 on a real record, and every golden question answered by the
  fallback.

The golden set is also the live benchmark input (plan §6): run each question with the provider configured and
compare `toolsUsed[0]` with `tool`, and `refused` with `refuse`.
