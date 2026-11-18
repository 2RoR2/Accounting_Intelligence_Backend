import test from "node:test";
import assert from "node:assert/strict";
import { CURRENCY_SUPPORT, normalizeValidationResult, reviewableFinding, validationState, validationSummary, processingSteps, validationRank, alternativeCandidates, dateReadings, dollarCurrencyChoices, fieldGroup, aiAssistCanApply, aiAssistOutcomeLabel, candidateText, modelIdentity, mapExtraction, fieldLabel, fieldCandidates } from "../src/lib/api/document-contract";
test("qualitative source warnings survive mapping without invented confidence", () => {
  const result = mapExtraction({documentId:"synthetic", warnings:[{code:"NO_MAPPED_LINE_ITEMS",message:"Inspect source"}],
    fields:[{key:"date",state:"ambiguous",confidence:null,value:null,confirmationRequired:true,
      warnings:[{code:"AMBIGUOUS_DATE",message:"Confirm date"}]}]});
  assert.equal(result.fields.date.confidence,null);
  assert.equal(result.fields.date.warnings?.[0].code,"AMBIGUOUS_DATE");
  assert.equal(result.fields.date.confirmationRequired,true);
  assert.equal(result.warnings?.length,1);
});
test("bill total and amount due have distinct accountant labels", () => {
  assert.equal(fieldLabel("BILL","total"),"Bill total");
  assert.equal(fieldLabel("BILL","amount_due"),"Amount due");
});

test("line correction keeps the displayed revision and exact string value", async () => {
  const {updateExtractedLineItem} = await import("../src/lib/api/document.service");
  const previous = globalThis.fetch; let url = ""; let body = "";
  globalThis.fetch = async (input, init) => {url=String(input); body=String(init?.body); return Response.json({lineItemId:"line",fieldKey:"quantity",value:"1.000",correctedBy:"actor",contentRevision:8});};
  try {
    await updateExtractedLineItem({role:"ACCOUNTANT"} as import("../src/types").User,"synthetic","line","quantity","1",7);
    assert.match(url,/line-items\/line\/quantity$/);
    assert.deepEqual(JSON.parse(body),{value:"1",expectedContentRevision:7});
  } finally {globalThis.fetch=previous;}
});
test("image upload sends original bytes while unsupported financial-document formats are refused", async () => {
  const {uploadDocument} = await import("../src/lib/api/document.service");
  const previous=globalThis.fetch; let file: File | undefined;
  globalThis.fetch=async (_input,init)=>{file=(init?.body as FormData).get("file") as File;return Response.json({id:"synthetic",kind:"RECEIPT"});};
  try {
    const user={role:"ACCOUNTANT"} as import("../src/types").User;
    await uploadDocument(user,new File([new Uint8Array([255,216,255])],"synthetic.jpg",{type:"image/jpeg"}),"real","ACCOUNTS_PAYABLE","RECEIPT");
    assert.equal(file?.type,"image/jpeg");assert.equal(file?.size,3);
    await assert.rejects(uploadDocument(user,new File(["x"],"synthetic.csv"),"real"));
  } finally {globalThis.fetch=previous;}
});
test("line states, warnings and persisted actor history survive response mapping",()=>{
  const history={line_item_id:"line",field_key:"quantity",old_value:null,new_value:"1",corrected_by:"actor",corrected_at:"2026-10-27T00:00:00Z"};
  const result=mapExtraction({documentId:"synthetic",lineCorrections:[history],lineItems:[{id:"line",quantity:"1.000",fieldStates:{quantity:"missing"},guidance:{quantity:{warnings:[{code:"MISSING_VALUE",message:"Inspect source"}]}}}]});
  assert.deepEqual(result.lineCorrections,[history]);
  assert.equal(result.lineItems[0].fieldStates?.quantity,"missing");
  assert.equal(result.lineItems[0].guidance?.quantity.warnings?.[0].code,"MISSING_VALUE");
});
test("source candidates and model suggestions stay separate and are never the field value",()=>{
  const result=mapExtraction({documentId:"synthetic",fields:[{key:"subtotal",state:"missing",value:null,confidence:null,evidenceStatus:"model_suggested",
    candidates:[{value:"725.37",raw_text:"$ 725,37"},{value:"725.37",raw_text:"$ 725,37"},{source:"ai",value:"999.00",auto_accepted:false,model:"local"},{value:null,raw_text:null}]}]});
  const field=result.fields.subtotal;
  assert.equal(field.value,null);
  assert.equal(field.confidence,null);
  assert.equal(field.evidenceStatus,"model_suggested");
  const split=fieldCandidates(field);
  assert.deepEqual(split.source.map(item=>item.value),["725.37"]);
  assert.deepEqual(split.model.map(item=>item.value),["999.00"]);
});
test("unknown classification retains provisional header and line candidates without selecting them",()=>{
  const candidate={value:"42.00",raw_text:"Total 42.00",evidence:[{source_ref:"#/texts/1",page:1,candidate_basis:"provisional_document_type"}]};
  const result=mapExtraction({documentId:"synthetic",classification:{value:"unknown",status:"unknown"},
    fields:[{key:"total",value:null,confidence:null,state:"ambiguous",candidates:[candidate]}],
    lineItems:[{id:"line",amount:null,fieldStates:{amount:"ambiguous"},candidates:{amount:[candidate]}}]});
  assert.equal(result.classification?.value,"unknown");
  assert.equal(result.fields.total.value,null);
  assert.equal(result.fields.total.confidence,null);
  assert.equal(result.lineItems[0].amount,null);
  assert.deepEqual(fieldCandidates({candidates:result.lineItems[0].candidates?.amount}).source,[candidate]);
});
test("the selected value is not listed as an alternative, but a different reading is",()=>{
  const candidates=[{value:"725.37",raw_text:"725,37"},{value:"999.00",raw_text:"999.00"},{value:null,raw_text:"$ 725,37"},{value:"725.37",source:"ai"}];
  const split=alternativeCandidates({value:"725.37",candidates});
  assert.deepEqual(split.source.map(item=>item.raw_text),["999.00","$ 725,37"]);
  assert.equal(split.model.length,1);
});
test("an ambiguous printed date offers both readings and picks neither",()=>{
  assert.deepEqual(dateReadings("04/09/2014"),[{label:"Month first",value:"2014-04-09"},{label:"Day first",value:"2014-09-04"}]);
  assert.deepEqual(dateReadings("Date: 12.08.2013"),[{label:"Month first",value:"2013-12-08"},{label:"Day first",value:"2013-08-12"}]);
  for (const printed of ["25/09/2014","04/04/2014","Sep 4 2014","104/09/2014","",null]) assert.deepEqual(dateReadings(printed),[]);
});
test("dollar currency choices appear only for an unresolved printed dollar sign",()=>{
  const dollar=[{value:null,raw_text:"$ 725,37"}];
  assert.ok(dollarCurrencyChoices("currency",{value:null,candidates:dollar}).includes("USD"));
  assert.deepEqual(dollarCurrencyChoices("currency",{value:"USD",candidates:dollar}),[]);
  assert.deepEqual(dollarCurrencyChoices("currency",{value:null,candidates:[]}),[]);
  assert.deepEqual(dollarCurrencyChoices("total",{value:null,candidates:dollar}),[]);
});
test("field groups order review by what needs a decision",()=>{
  assert.equal(fieldGroup("total",{evidenceStatus:"source_backed"},[]),"source_backed");
  assert.equal(fieldGroup("date",{evidenceStatus:"source_backed"},["invoice_date"]),"attention");
  assert.equal(fieldGroup("date",{evidenceStatus:"human_corrected",correctedBy:"u"},["invoice_date"]),"source_backed");
  assert.equal(fieldGroup("due_date",{evidenceStatus:"missing"},[]),"not_printed");
  assert.equal(fieldGroup("currency",{evidenceStatus:"missing"},["currency"]),"attention");
  assert.equal(fieldGroup("subtotal",{evidenceStatus:"ambiguous_candidate"},[]),"not_printed");
  assert.equal(fieldGroup("currency",{evidenceStatus:"ambiguous_candidate"},["currency"]),"attention");
  for (const evidenceStatus of ["conflicting","model_suggested","unsupported"] as const) assert.equal(fieldGroup("x",{evidenceStatus},[]),"attention");
});
test("processing steps state what actually ran",()=>{
  const ocr=processingSteps({text_lines:54,ocr_lines:54,runner:"langgraph",stages:["a","b"],final_route:"ACCOUNTANT_ACTION_REQUIRED",qwen:{attempted:true,fields:[{field:"transaction_time"}]}});
  assert.deepEqual(ocr,["Docling with RapidOCR: 54 of 54 text lines came from OCR","LangGraph workflow: 2 stages ran","Qwen2.5-VL asked about 1 field","Route: Accountant action required"]);
  const pdf=processingSteps({text_lines:35,ocr_lines:0,runner:"plain",runner_reason:"LANGGRAPH_NOT_INSTALLED:langgraph",stages:["a"],qwen:{attempted:false}});
  assert.equal(pdf[0],"Docling: 35 text lines from the PDF text layer, no OCR needed");
  assert.match(pdf[1],/Plain router.*LANGGRAPH_NOT_INSTALLED/);
  assert.match(pdf[2],/not called/);
  assert.deepEqual(processingSteps(null),[]);
  assert.match(processingSteps({})[0],/not recorded/);
});
test("validation rank puts blocking findings first and hides nothing",()=>{
  const items=[{result:"PASS"},{result:"WARNING"},{result:"FAIL"},{result:"WARNING",preventsApproval:true},{result:"FAIL",preventsApproval:true}] as const;
  assert.deepEqual(items.map(validationRank),[4,3,2,1,0]);
});
test("a bare dollar candidate keeps its printed text without a currency value",()=>{
  const split=fieldCandidates({candidates:[{value:null,raw_text:"$ 725,37"}]});
  assert.deepEqual(split.source.map(item=>item.raw_text),["$ 725,37"]);
  assert.equal(split.source[0].value,null);
});
test("the review screen says confidence is unmeasured and labels OCR scores as recognition only",async ()=>{
  const {readFile}=await import("node:fs/promises");
  const source=await readFile(new URL("../src/features/common/documents/document-review.tsx",import.meta.url),"utf8");
  assert.match(source,/"Not measured"/);
  assert.match(source,/text recognition only, not field correctness/);
  assert.match(source,/Model suggestion, not verified as an accounting fact/);
});
test("a printed tax-exclusive candidate shows its label, printed value and page for the reviewer",()=>{
  const text=candidateText({value:"725.37",evidence:[{page:1,printed_label:"Net worth",printed_value:"$ 725,37",extraction_method:"OCR",candidate_basis:"printed_tax_exclusive_total"}]});
  assert.equal(text,'725.37 (printed as Net worth, reads "$ 725,37", page 1, OCR)');
});
test("a model suggestion says its text check does not verify the field role",()=>{
  assert.match(candidateText({source:"ai",value:"12.00",model:"qwen2.5vl:7b",source_check:"source_text_match"}),/role not verified/);
});
test("review prevents revalidating over unsaved edits and candidates need an explicit save",async ()=>{
  const {readFile}=await import("node:fs/promises");
  const source=await readFile(new URL("../src/features/common/documents/document-review.tsx",import.meta.url),"utf8");
  assert.match(source,/Revalidation uses saved values only/);
  assert.equal((source.match(/disabled=\{(?:readonly \|\| )?busy \|\| unsaved\.length > 0\}/g)??[]).length,2);
  assert.match(source,/Fills the input only/);
});
test("AI Assist never offers a rejected or empty result for saving and never calls a suggestion verified",()=>{
  const row=(outcome: import("../src/types").AiAssistOutcome,candidate: string|null)=>({field:"f",stored_key:"f",outcome,candidate,explanation:""});
  assert.equal(aiAssistCanApply(row("SOURCE_VERIFIED_CANDIDATE","1.00")),true);
  assert.equal(aiAssistCanApply(row("UNVERIFIED_MODEL_SUGGESTION","1.00")),true);
  assert.equal(aiAssistCanApply(row("NO_RECOVERY",null)),false);
  assert.equal(aiAssistCanApply(row("MODEL_FAILURE",null)),false);
  assert.equal(aiAssistCanApply(row("PARSER_PRESERVED","1.00")),false);
  assert.equal(aiAssistOutcomeLabel("UNVERIFIED_MODEL_SUGGESTION"),"Unverified suggestion");
  assert.doesNotMatch(aiAssistOutcomeLabel("UNVERIFIED_MODEL_SUGGESTION"),/source-checked/i);
});
test("model identity shows the model, a short digest, the page and time, and no confidence",()=>{
  const text=modelIdentity({model:"qwen2.5vl:7b",model_digest:"5ced39dfa4bac325dc18",page:1,seconds:0.5});
  assert.equal(text,"qwen2.5vl:7b · digest 5ced39dfa4ba · page 1 · 0.5 s");
  assert.doesNotMatch(text,/%|confidence/i);
});

test("the processing panel states the image actually sent to the model", () => {
  const steps = processingSteps({ text_lines: 42, ocr_lines: 42, runner: "langgraph", stages: ["a"], qwen: { attempted: true, fields: [{ field: "merchant" }] },
    visual_input: { kind: "full_page", page: 1, width: 920, height: 1300, prompt_tokens: 1984 } });
  assert.ok(steps.some(step => step === "Image sent to the model: page 1, 920 x 1300 px, 1984 prompt tokens with the image"));
});

test("no image line is shown when the model was not called", () => {
  const steps = processingSteps({ text_lines: 42, ocr_lines: 42, runner: "langgraph", stages: ["a"], qwen: { attempted: false, fields: [] } });
  assert.ok(!steps.some(step => step.startsWith("Image sent")));
});

test("a model candidate is never grouped as source-backed", () => {
  for (const evidenceStatus of ["model_source_verified", "model_suggested"] as const) {
    assert.equal(fieldGroup("reference", { evidenceStatus, correctedBy: null }, []), "attention");
  }
});

test("the four validation states stay distinct, and only open findings can be settled by review", () => {
  const applicable = normalizeValidationResult({ rule: "EX-009", result: "NOT_APPLICABLE", message: "x", checkStatus: "NOT_APPLICABLE" });
  assert.equal(applicable.result, "NOT_APPLICABLE");
  assert.equal(validationState(applicable), "NOT_APPLICABLE");
  const notChecked = normalizeValidationResult({ rule: "EX-007", result: "NOT_CHECKED", message: "x", checkStatus: "NOT_CHECKED", preventsApproval: true });
  const summary = validationSummary([applicable, notChecked, normalizeValidationResult({ rule: "A", result: "PASSED", message: "x" }), normalizeValidationResult({ rule: "B", result: "FAILED", message: "x" })]);
  assert.deepEqual(summary, { passed: 1, failed: 1, notChecked: 1, notApplicable: 1, warnings: 0, blocking: 2 });
  assert.ok(validationRank(applicable) > validationRank(notChecked));
  assert.equal(reviewableFinding({ rule: "EX-010", result: "FAIL" }), true);
  assert.equal(reviewableFinding({ rule: "EX-010", result: "PASS", checkStatus: "CHECKED" }), false);
  assert.equal(reviewableFinding({ rule: "TAX-STATUS", result: "WARNING", checkStatus: "NOT_CHECKED", preventsApproval: true }), true);
  assert.equal(reviewableFinding({ rule: "TAX-STATUS", result: "PASS", checkStatus: "CHECKED" }), false);
  assert.equal(reviewableFinding({ rule: "EX-007", result: "FAIL" }), false);
});

test("currency proposals reach the review page as advice, with no invented confidence", () => {
  const review = { version: "currency-review/1", needed: true, printedSymbol: { symbol: "$", text: "$ 124,79" }, alternatives: ["SGD"], conflicts: [], disagreement: false, sourceVerified: false as const, statement: "A suggestion to check, not evidence.",
    proposals: [{ rank: 1, currency: "USD", status: "proposed_not_verified" as const, support: "document_context" as const, clues: [{ kind: "supplier_address", name: "Supplier address on the document", currency: "USD", text: "OH 49620", why: "US state and ZIP code", page: 1 }] }] };
  const result = mapExtraction({ documentId: "synthetic", currencyReview: review, fields: [{ key: "currency", state: "ambiguous", value: null, confidence: null }] });
  assert.equal(result.fields.currency.value, null);
  assert.equal(result.fields.currency.confidence, null);
  assert.equal(result.currencyReview?.proposals[0].currency, "USD");
  assert.equal(result.currencyReview?.sourceVerified, false);
  assert.equal(CURRENCY_SUPPORT.document_context, "Inferred from context only");
  assert.ok(!JSON.stringify(result.currencyReview).includes("confidence"));
  assert.equal(mapExtraction({ documentId: "older" }).currencyReview, null);
});
