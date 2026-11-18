import { test } from "node:test";
import assert from "node:assert/strict";
import { retryDocumentProcessing } from "../src/lib/api/document.service";
import { ApiError } from "../src/lib/api/client";
import type { User } from "../src/types";
const user={role:"ACCOUNTANT"} as User;
const receipt={id:"document",jobId:"processing-job",queueJobId:"queue-job",status:"QUEUED",jobStatus:"DISPATCHED",dispatchAccepted:true};
async function withResponse(body:unknown,status:number,check:()=>Promise<void>){const old=globalThis.fetch;let calls=0;globalThis.fetch=async(_url,init)=>{calls++;assert.equal(init?.method,"POST");return Response.json(body,{status});};try{await check();assert.equal(calls,1);}finally{globalThis.fetch=old;}}
test("retry accepts actual success identities and dispatch receipt",async()=>{await withResponse(receipt,200,async()=>assert.deepEqual(await retryDocumentProcessing(user,"document"),receipt));});

test("retry accepts a durable pending receipt without claiming dispatch or extraction",async()=>{const body={...receipt,queueJobId:null,jobStatus:"PENDING",dispatchAccepted:false};await withResponse(body,200,async()=>assert.deepEqual(await retryDocumentProcessing(user,"document"),body));});
test("retry allows nullable or omitted queue ID without losing DB identity",async()=>{for(const queueJobId of [null,undefined]){const body={...receipt,queueJobId};await withResponse(body,200,async()=>{const result=await retryDocumentProcessing(user,"document");assert.equal(result.jobId,receipt.jobId);assert.equal(result.queueJobId,queueJobId);});}});
test("retry rejects missing identities or unconfirmed dispatch without replay",async()=>{for(const body of [null,{},...['id','jobId','status','jobStatus','dispatchAccepted'].map(key=>({...receipt,[key]:undefined})),{...receipt,id:'another-document'},{...receipt,queueJobId:3},{...receipt,dispatchAccepted:false}])await withResponse(body,200,async()=>{await assert.rejects(retryDocumentProcessing(user,"document"),/not confirmed/);});});
test("retry acceptance does not imply queued state or processing success",async()=>{for(const jobStatus of ['PROCESSING','SUCCEEDED','FAILED'])await withResponse({...receipt,status:'FAILED',jobStatus},200,async()=>{assert.equal((await retryDocumentProcessing(user,"document")).jobStatus,jobStatus);});});
for(const [status,code] of [[409,'JOB_ALREADY_OPEN'],[409,'AMENDMENT_REQUIRED'],[403,'FORBIDDEN'],[422,'INVALID_STATE'],[503,'DISPATCH_UNCONFIRMED']] as const){test('retry preserves '+status+' '+code+' with no automatic replay',async()=>{await withResponse({detail:{code,message:'Actual server refusal',documentId:'document',jobId:'processing-job',dispatchAccepted:false}},status,async()=>{await assert.rejects(retryDocumentProcessing(user,'document'),error=>{assert.ok(error instanceof ApiError);assert.equal(error.status,status);assert.equal(error.details?.code,code);assert.equal(error.message,'Actual server refusal');return true;});});});}
