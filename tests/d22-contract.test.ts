import { test } from "node:test";
import assert from "node:assert/strict";
import { mapExtraction, sourceKind, fieldLabel, validationState, standardisationAvailable, type ValidationReport } from "../src/lib/api/document-contract";

test("D2.2 explicit unknown overrides stored invoice and keeps accounting fields empty", () => {
  const extraction=mapExtraction({documentId:"synthetic",classification:{value:"unknown",status:"unknown",reason:"NO_SUPPORTED_DOCUMENT_HEADING"},raw:{document_type:{value:"invoice"}},fields:[],lineItems:[]});
  assert.equal(sourceKind(extraction,"INVOICE"),"UNKNOWN");assert.deepEqual(extraction.fields,{});assert.equal(extraction.classification?.reason,"NO_SUPPORTED_DOCUMENT_HEADING");
});
test("D2.2 classified bill uses issuer/account/due labels and preserves null charges", () => {
  const evidence=[{page:1,source_ref:"#/texts/1",text:"Total: 80.00",text_span:[0,17] as [number,number],bbox_scope:"source_block"}];
  const extraction=mapExtraction({documentId:"bill",classification:{value:"bill",status:"classified",reason:"EXPLICIT_DOCUMENT_HEADING"},fields:[{key:"total",value:"80.00",confidence:null,evidence},{key:"tax",value:null}],lineItems:[{quantity:null,unit_price:null,amount:"80.00"}]});
  assert.equal(sourceKind(extraction,"INVOICE"),"BILL");assert.equal(fieldLabel("BILL","supplier"),"Issuer");
  assert.equal(fieldLabel("BILL","total"),"Bill total");assert.equal(fieldLabel("BILL","amount_due"),"Amount due");assert.equal(extraction.fields.tax.value,null);
  assert.deepEqual(extraction.fields.total.evidence,evidence);assert.equal(extraction.lineItems[0].quantity,null);assert.equal(extraction.lineItems[0].unitPrice,null);
});
test("D2.2 receipt payment method is source text, never a paid status", () => {
  assert.equal(fieldLabel("RECEIPT","supplier"),"Merchant");assert.equal(fieldLabel("RECEIPT","transaction_time"),"Transaction time");
  const extraction=mapExtraction({documentId:"receipt",classification:{value:"receipt",status:"classified"},fields:[{key:"payment_method",value:"Card"},{key:"transaction_time",value:"14:35:20"},{key:"total",value:"10.60"}]});
  assert.equal(extraction.fields.payment_method.value,"Card");assert.equal(extraction.fields.payment_method.confidence,null);assert.equal(extraction.fields.transaction_time.value,"14:35:20");assert.equal(extraction.fields.total.value,"10.60");
});
test("D2.2 deferred validation remains NOT_CHECKED and cannot enable standardisation", () => {
  const report={validationStatus:"REVIEW_REQUIRED",coverageComplete:false,blocking:false} as ValidationReport;
  assert.equal(validationState({rule:"BILL_FINANCIAL_MATH",result:"WARNING",checkStatus:"NOT_CHECKED",message:"Actual backend check unavailable"}),"NOT_CHECKED");
  assert.equal(standardisationAvailable("VALIDATED","BILL",report),false);
  assert.equal(standardisationAvailable("VALIDATED","UNKNOWN",{...report,validationStatus:"PASS",coverageComplete:true}),false);
  assert.equal(standardisationAvailable("EXTRACTED","INVOICE",{...report,validationStatus:"PASS",coverageComplete:true}),false);
  assert.equal(standardisationAvailable("VALIDATED","INVOICE",{...report,validationStatus:"PASS",coverageComplete:true}),true);
});
test("D2.2 legacy raw classification and approved record fields remain compatible", () => {
  const extraction=mapExtraction({documentId:"legacy",raw:{document_type:{value:"invoice",status:"classified"}},fields:[{key:"total",value:"0.00",normalizedValue:"99.00",correctedBy:"synthetic-reviewer"}]});
  assert.equal(sourceKind(extraction,"BILL"),"INVOICE");assert.equal(extraction.fields.total.value,"0.00");assert.equal(extraction.fields.total.original,"99.00");
  assert.equal(sourceKind({classification:undefined},"CREDIT_NOTE"),"CREDIT_NOTE");
});
