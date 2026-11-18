import { test } from "node:test";
import assert from "node:assert/strict";
import { decimalDisplay, mapDocument, mapExtraction, mapRecord, type RecordResponse } from "../src/lib/api/document-contract";
import { getValidationReport, updateExtractedField, uploadDocument } from "../src/lib/api/document.service";
import type { Document, User } from "../src/types";
const user = {id:"synthetic-reviewer",role:"LOCAL_ADMIN"} as User;
test("D1A extraction keeps nulls, original values, evidence and revision", () => {
  const evidence=[{page:1,source_ref:"#/texts/7",text:"Total 99.00"}];
  const result=mapExtraction({documentId:"d",contentRevision:3,raw:{document_type:{value:"invoice",status:"classified"}},
    fields:[{key:"total",value:"50.00",rawValue:"Total 99.00",normalizedValue:"99.00",currentValue:"50.00",confidence:null,evidence,correctedBy:"reviewer"},
            {key:"currency",value:null,confidence:null}],lineItems:[{quantity:"2.000",unit_price:"25.0000",amount:"50.00"}]});
  assert.equal(result.contentRevision,3); assert.equal(result.fields.total.original,"99.00");
  assert.equal(result.fields.currency.value,null); assert.equal(result.fields.total.confidence,null);
  assert.deepEqual(result.fields.total.evidence,evidence); assert.equal(result.lineItems[0].unitPrice,"25.0000");
  assert.equal(result.classification?.value,"invoice");
});
test("zero, missing amounts and large decimal strings remain distinct", () => {
  assert.equal(decimalDisplay(null),"Not provided"); assert.equal(decimalDisplay("0.00"),"0.00");
  assert.equal(decimalDisplay("999999999999.99"),"999,999,999,999.99");
  assert.equal(decimalDisplay("25.0000","MYR"),"MYR 25.0000");
  assert.equal(decimalDisplay("NaN"),"Invalid amount");
});
test("backend kind becomes existing UI type without an AP/AR inference", () => {
  const result=mapDocument({kind:"INVOICE",category:"accounts_receivable"} as unknown as Document);
  assert.equal(result.type,"INVOICE"); assert.equal((result as unknown as {category:string}).category,"accounts_receivable");
});
test("approved snapshot maps reviewed field DTOs rather than raw extraction fields", () => {
  const record=mapRecord({id:"r",tenantId:"t",createdAt:"2026-10-19",category:"ACCOUNTS_PAYABLE",validation:"PASSED",status:"COMPLETED",documentId:"d",snapshot:{contentRevision:3,
    fieldPredictions:[{key:"total",value:"50.00",normalizedValue:"99.00"}],
    lineItems:[{quantity:"2",unit_price:"25.0000",amount:"50.00"}], extraction:{fields:{total_amount:{value:"99.00"}}}}} as RecordResponse);
  assert.equal(record.extraction.fields.total.value,"50.00");
  assert.equal(record.extraction.fields.total.original,"99.00");
});
test("correction sends the displayed revision and retains the returned revision", async () => {
  const previous=globalThis.fetch; let sent: RequestInit | undefined;
  globalThis.fetch=async (_input,init) => {sent=init; return Response.json({key:"total",value:"50.00",correctedBy:"r",contentRevision:3});};
  try { const result=await updateExtractedField(user,"d","total","50.00",2);
    assert.deepEqual(JSON.parse(sent?.body as string),{value:"50.00",expectedContentRevision:2});
    assert.equal(result.contentRevision,3);
  } finally {globalThis.fetch=previous;}
});
test("validation is the actual backend report rather than client money arithmetic", async () => {
  const previous=globalThis.fetch; const paths:string[]=[];
  globalThis.fetch=async input=>{paths.push(String(input));return Response.json({validationStatus:"REVIEW_REQUIRED",blocking:true,results:[{rule:"EX-005",result:"FAIL",message:"Actual backend mismatch"}]});};
  try {const result=await getValidationReport(user,"d"); assert.equal(result.results[0].rule,"EX-005");
    assert.equal(paths.length,1); assert.match(paths[0],/\/documents\/d\/validation$/);
  } finally {globalThis.fetch=previous;}
});
test("upload passes direction and kind as query parameters with actual file bytes", async () => {
  const previous=globalThis.fetch; let url="";let body:FormData | undefined;
  globalThis.fetch=async (input,init)=>{url=String(input);body=init?.body as FormData;return Response.json({id:"d",kind:"INVOICE",queueJobId:"q"});};
  try {await uploadDocument(user,new File(["%PDF synthetic"],"synthetic.pdf",{type:"application/pdf"}),"real","ACCOUNTS_RECEIVABLE","INVOICE");
    assert.match(url,/kind=invoice/);assert.match(url,/category=accounts_receivable/);
    assert.equal((body?.get("file") as File).size,14);assert.equal(body?.get("kind"),null);
  } finally {globalThis.fetch=previous;}
});
