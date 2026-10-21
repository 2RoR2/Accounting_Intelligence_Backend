/* Narrow Phase4CA browser contract. Real UI/API/PG; synthetic stored extraction. */
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const B=path.resolve(__dirname,'../..'),F=path.join(path.dirname(B),'Accounting_Intelligence_Frontend_V2_integration_20261020');
const {chromium,expect}=require(path.join(F,'node_modules/@playwright/test'));
const E=path.join(B,'runs/phase4a-20261021a1/evidence/phase4ca'),P=path.join(B,'runs/phase4a-20261021a1/private/phase4ca-20261021');
const accounts=JSON.parse(fs.readFileSync(path.join(P,'accounts.json'),'utf8'));
const fixture=JSON.parse(fs.readFileSync(path.join(E,'browser-fixture.json'),'utf8'));
const base='http://127.0.0.1:3117',doc=fixture.document,root='/api/documents/'+doc;
const results=[],http=[];let browser,page;
function record(name,status,details={}){results.push({name,status,...details});fs.writeFileSync(path.join(E,'browser-results.json'),JSON.stringify({results,http},null,2));console.log(JSON.stringify(results.at(-1)));}
function totp(secret){const alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';let bits='';for(const c of secret.toUpperCase().replace(/=+$/,''))bits+=alphabet.indexOf(c).toString(2).padStart(5,'0');const bytes=[];for(let i=0;i+8<=bits.length;i+=8)bytes.push(parseInt(bits.slice(i,i+8),2));const counter=Buffer.alloc(8);counter.writeBigUInt64BE(BigInt(Math.floor(Date.now()/30000)));const digest=crypto.createHmac('sha1',Buffer.from(bytes)).update(counter).digest();const offset=digest.at(-1)&15;return ((digest.readUInt32BE(offset)&0x7fffffff)%1000000).toString().padStart(6,'0');}
async function context(){const ctx=await browser.newContext({viewport:{width:1440,height:1000}});await ctx.route('**/*',route=>{const u=new URL(route.request().url());return (u.hostname==='127.0.0.1'&&u.port==='3117')||['data:','blob:','about:','chrome-extension:'].includes(u.protocol)?route.continue():route.abort();});return ctx;}
function observe(p){p.setDefaultTimeout(30000);p.on('response',r=>{const u=new URL(r.url());if(u.pathname.startsWith('/api/'))http.push({path:u.pathname,method:r.request().method(),status:r.status()});});}
async function login(ctx,label){const u=accounts[label];page=await ctx.newPage();observe(page);await page.goto(base+'/login');await page.locator('input[type=email]').fill(u.email);await page.locator('input[autocomplete=current-password]').fill(u.password);const pending=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/auth/login'&&r.request().method()==='POST');await page.getByRole('button',{name:'Continue with email',exact:true}).click();const response=await pending;expect(response.status()).toBe(200);const challenge=await response.json();expect(challenge.mfaRequired).toBe(true);let code;
 if(challenge.mfaMethod==='email'){const out=path.join(P,'outbox',crypto.createHash('sha256').update(u.email).digest('hex')+'.json');await expect.poll(()=>fs.existsSync(out),{timeout:10000}).toBe(true);code=JSON.parse(fs.readFileSync(out,'utf8')).code;}
 else {expect(typeof challenge.totpSecret).toBe('string');code=totp(challenge.totpSecret);}
 for(let i=0;i<6;i++)await page.getByRole('textbox',{name:'Authenticator digit '+(i+1),exact:true}).fill(code[i]);const verified=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/auth/verify-mfa');await page.getByRole('button',{name:'Verify and continue',exact:true}).click();expect((await verified).status()).toBe(200);await page.waitForURL('**/welcome?**',{timeout:45000});await ctx.storageState({path:path.join(P,label+'-session.json')});record(label+'-genuine-MFA','PASS',{method:challenge.mfaMethod||'TOTP',role:u.role});return page;}
async function request(ctx,method,url,body){const r=await ctx.request[method](base+url,body?{data:body,headers:{'X-Requested-With':'AccountingIntelligence'}}:undefined);return r;}
async function save(p,label,value,url){const input=p.getByRole('textbox',{name:label,exact:true});await expect(input).toBeEnabled({timeout:45000});await input.fill(value);const pending=p.waitForResponse(r=>new URL(r.url()).pathname===url&&r.request().method()==='PATCH');await p.getByRole('button',{name:'Save '+label,exact:true}).click();const r=await pending;expect(r.status()).toBe(200);return r.json();}
(async()=>{try{
 browser=await chromium.launch({channel:'chrome',headless:true});
 const ctx=await context();page=await login(ctx,'owner');
 let r=await request(ctx,'get',root+'/reviews');expect(r.status()).toBe(200);expect(await r.json()).toEqual({submissions:[],decisions:[]});record('history-empty-real-GET','PASS',{http:200,document:doc});
 await page.goto(base+'/workspace/documents/'+doc);await expect(page.getByRole('textbox',{name:'Line 1 Description',exact:true})).toBeEnabled({timeout:60000});
 // Explicitly inspect the ancillary history response consumed by the real UI.
 expect(http.some(x=>x.path===root+'/reviews'&&x.status===200)).toBe(true);expect(http.some(x=>x.path===root+'/reviews'&&x.status>=500)).toBe(false);
 await page.screenshot({path:path.join(E,'screenshots/01-review-history-enabled.png'),fullPage:true});record('browser-review-controls-enabled','PASS');
 const header=page.getByRole('textbox',{name:'Invoice number',exact:true});expect(await header.count()).toBe(1);
 const changed=await save(page,'Invoice number','P4CA-REVIEWED',root+'/extraction/invoice');expect(changed.contentRevision).toBe(3);
 await page.reload();await expect(page.getByRole('textbox',{name:'Invoice number',exact:true})).toHaveValue('P4CA-REVIEWED',{timeout:60000});record('browser-header-save-reload','PASS',{http:200,revision:3});
 r=await request(ctx,'patch',root+'/extraction/invoice',{value:'P4CA-STALE',expectedContentRevision:2});expect(r.status()).toBe(409);record('stale-header-rejected','PASS',{http:409});
 const line=await save(page,'Line 1 Description','Synthetic review-approved stationery',root+'/line-items/'+(await (await request(ctx,'get',root+'/extraction')).json()).lineItems[0].id+'/description');expect(line.contentRevision).toBe(4);record('browser-line-correction','PASS',{http:200,revision:4});
 await page.reload();await expect(page.getByRole('textbox',{name:'Line 1 Description',exact:true})).toHaveValue('Synthetic review-approved stationery',{timeout:60000});
 const validation=page.waitForResponse(r=>new URL(r.url()).pathname===root+'/revalidate'&&r.request().method()==='POST');await page.getByRole('button',{name:'Save & revalidate',exact:true}).click();expect((await validation).status()).toBe(200);record('browser-revalidate','PASS',{http:200});
 const submit=page.waitForResponse(r=>new URL(r.url()).pathname===root+'/review'&&r.request().method()==='POST');await page.getByRole('button',{name:'Submit for approval',exact:true}).click();expect((await submit).status()).toBe(200);
 r=await request(ctx,'get',root+'/reviews');expect(r.status()).toBe(200);let history=await r.json();expect(history.submissions).toHaveLength(1);expect(history.submissions[0].submittedBy).toBe(accounts.owner.id);expect(history.decisions).toHaveLength(0);record('browser-submission-history','PASS',{http:200,actor:accounts.owner.id});
 await page.screenshot({path:path.join(E,'screenshots/02-submitted-review.png'),fullPage:true});
 const reviewer=await context();page=await login(reviewer,'reviewer');await page.goto(base+'/company/documents/'+doc);await expect(page.getByRole('button',{name:'Approve document',exact:true})).toBeEnabled({timeout:60000});
 const approval=page.waitForResponse(r=>new URL(r.url()).pathname===root+'/review/decision'&&r.request().method()==='POST');await page.getByRole('button',{name:'Approve document',exact:true}).click();expect((await approval).status()).toBe(200);
 r=await request(reviewer,'get',root+'/reviews');expect(r.status()).toBe(200);history=await r.json();expect(history.decisions).toHaveLength(1);expect(history.decisions[0]).toMatchObject({reviewedBy:accounts.reviewer.id,decision:'APPROVED',approvedRevision:4});expect(Number.isNaN(Date.parse(history.decisions[0].createdAt))).toBe(false);record('browser-distinct-reviewer-approved-history','PASS',{http:200,revision:4,reviewer:accounts.reviewer.id});
 await page.screenshot({path:path.join(E,'screenshots/03-approved-review.png'),fullPage:true});
 const foreign=await context();page=await login(foreign,'foreign');r=await request(foreign,'get',root+'/reviews');expect(r.status()).toBe(404);record('browser-session-cross-tenant-history-denied','PASS',{http:404});
 }catch(error){record('focused-browser-prerequisite','FAIL',{diagnostic:String(error.message).slice(0,1800)});if(page)await page.screenshot({path:path.join(E,'screenshots/failure.png'),fullPage:true,mask:[page.locator('input'),page.locator('svg[aria-label="Authenticator QR code"]')]}).catch(()=>{});process.exitCode=1;
 }finally{if(browser)await browser.close();fs.writeFileSync(path.join(E,'browser-results.json'),JSON.stringify({results,http},null,2));}
})();
