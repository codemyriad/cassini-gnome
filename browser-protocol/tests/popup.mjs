// Exercise the shipped popup DOM and module in a disposable browser. Only the
// Chrome transport is a fixture; this test never connects to a native host.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile, mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const server = createServer(async (request, response) => {
  const file = request.url?.slice(1);
  if (!['popup.html', 'popup.js', 'popup-state.mjs', 'tab-evidence.mjs'].includes(file)) {response.writeHead(404).end();return;}
  response.setHeader('Content-Type', file.endsWith('.html') ? 'text/html' : 'text/javascript');
  response.end(await readFile(path.join(root,'extension',file)));
});
await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
let browser;
try {
  browser = await chromium.launch({headless:true});
  const context = await browser.newContext({viewport:{width:376,height:800},permissions:['clipboard-read','clipboard-write']});
  await context.addInitScript(() => {
    window.fixture = {requests:[], copies:[], tab:{id:7,title:'Generated meeting <script>',url:'https://example.test/call/private'},
      status:{browserHealth:{extensionVersion:'0.2.20',nativeControl:'connected',worker:'ready',filesOk:true,tabs:[]}}};
    window.chrome = {
      tabs:{async query(){return [fixture.tab]}},
      runtime:{async sendMessage(message){
        fixture.requests.push(message);
        if(message.type==='recordRequest'){
          if(fixture.recordReply)return fixture.recordReply(message);
          return {error:'No record fixture'};
        }
        if(message.type==='reloadMeetingTab'){
          if(fixture.reloadError)return {error:fixture.reloadError};
          fixture.status.browserHealth.tabs=[];
        }
        if(fixture.statusError)throw Error(fixture.statusError);
        if(message.type==='status' && fixture.statusNeverResolves)return new Promise(()=>{});
        if(message.type==='status' && fixture.holdStatus){
          const value=structuredClone(fixture.status);fixture.holdStatus=false;
          return new Promise(resolve=>{window.releaseStatus=()=>resolve(value)});
        }
        if(message.type==='status' && fixture.captures){
          const capture=fixture.captures.find(c=>c.tabId===message.tabId);
          return {...structuredClone(fixture.status),...(capture||{recording:false,tabId:message.tabId})};
        }
        return structuredClone(fixture.status);
      }}
    };
  });
  const page = await context.newPage();
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto(`http://127.0.0.1:${server.address().port}/popup.html`);
  await page.locator('main[aria-busy="false"]').waitFor();
  assert.equal(await page.locator('#status').textContent(),'No meeting sources detected');
  assert.equal(await page.locator('#tab-title').textContent(),'Generated meeting <script>');
  // Without an opted-in app, Start is visible but disabled and says why.
  assert.equal(await page.locator('#record').isDisabled(),true);
  assert.match(await page.locator('#record-hint').textContent(),/cannot start recordings from the browser/);
  await recordFlow(page);
  await page.evaluate(()=>fixture.status.browserHealth.tabs=[{tabId:3,state:'reloadRequired'},{tabId:7,state:'ready'}]);
  await page.getByRole('heading',{name:'Meeting sources detected',exact:true}).waitFor();
  assert.equal(await page.locator('#reload').isVisible(),false);
  await page.evaluate(()=>{
    fixture.status.browserHealth.tabs=[{tabId:7,state:'reloadRequired'}];
    fixture.captures=[{tabId:3,recording:true,tracks:1},{tabId:7,recording:true,tracks:2}];
  });
  await page.getByRole('heading',{name:'Browser capture is active',exact:true}).waitFor();
  assert.equal(await page.locator('#reload').isVisible(),false,'a second active capture must not offer reload');
  assert.match(await page.locator('#recording-note').textContent(),/2 browser audio tracks/);
  assert.ok(await page.evaluate(()=>fixture.requests.filter(r=>r.type==='status').every(r=>r.tabId===7)));
  await page.evaluate(()=>{fixture.captures=[{tabId:3,recording:true,tracks:1}];fixture.status.browserHealth.tabs=[{tabId:7,state:'ready'}]});
  await page.getByRole('heading',{name:'Meeting sources detected',exact:true}).waitFor();
  await page.evaluate(()=>{fixture.captures=[{tabId:7,recording:false,starting:true}];fixture.status.browserHealth.tabs=[{tabId:7,state:'reloadRequired'}]});
  await page.getByRole('heading',{name:'Preparing browser capture…',exact:true}).waitFor();
  assert.equal(await page.locator('#reload').isVisible(),false);
  await page.evaluate(()=>{delete fixture.captures;fixture.status.browserHealth.tabs=[{tabId:3,state:'ready'},{tabId:7,state:'reloadRequired'}]});
  await page.getByRole('button',{name:'Reload meeting tab',exact:true}).waitFor();
  assert.match(await page.locator('#reload-warning').textContent(),/briefly disconnects/);
  await page.evaluate(()=>fixture.reloadError='Stop and save the recording before reloading its tab.');
  await page.locator('#reload').click();
  await page.getByText('Stop and save the recording before reloading its tab.',{exact:true}).waitFor();
  assert.equal(await page.locator('#reload').isEnabled(),true);
  // A backend recovery clears a refused action even without another click.
  await page.evaluate(()=>fixture.status.browserHealth.tabs=[{tabId:7,state:'ready'}]);
  await page.getByRole('heading',{name:'Meeting sources detected',exact:true}).waitFor();
  assert.equal(await page.locator('#action-error').isVisible(),false);
  await page.evaluate(()=>{delete fixture.reloadError;fixture.status.browserHealth.tabs=[{tabId:7,state:'reloadRequired'}]});
  await page.getByRole('button',{name:'Reload meeting tab',exact:true}).waitFor();
  await page.locator('#reload').click();
  await page.getByRole('heading',{name:'Tab reloaded · rejoin your call',exact:true}).waitFor();
  const polls=await page.evaluate(()=>fixture.requests.filter(r=>r.type==='status').length);
  await page.waitForFunction(count=>fixture.requests.filter(r=>r.type==='status').length>=count+2,polls);
  assert.equal(await page.locator('#status').textContent(),'Tab reloaded · rejoin your call','guidance survives refreshes without new evidence');
  await page.evaluate(()=>fixture.status.browserHealth.tabs=[{tabId:7,state:'ready'}]);
  await page.getByRole('heading',{name:'Meeting sources detected',exact:true}).waitFor();
  await page.evaluate(()=>fixture.status.browserHealth.tabs=[]);
  await page.getByRole('heading',{name:'No meeting sources detected',exact:true}).waitFor();
  assert.deepEqual(await page.evaluate(()=>fixture.requests.filter(r=>r.type==='reloadMeetingTab').map(r=>r.tabId)),[7,7]);
  await page.locator('summary').click();
  await page.locator('#copy').click();
  await page.getByText('Diagnostics copied.',{exact:true}).waitFor();
  const report=JSON.parse(await page.evaluate(()=>navigator.clipboard.readText()));
  assert.equal(report.schemaVersion,1);
  assert.equal(report.callEvidence.state,'unknown');
  assert.equal('currentTabId' in report,false,'copied diagnostics exclude tab and document identities');
  assert.doesNotMatch(JSON.stringify(report),/private|Generated meeting/);
  await mkdir(path.join(root,'artifacts'),{recursive:true});
  await page.screenshot({path:path.join(root,'artifacts/popup-idle.png'),fullPage:true});
  await page.emulateMedia({colorScheme:'dark'});
  await page.evaluate(()=>fixture.status.browserHealth.filesOk=false);
  await page.locator('#installation-warning').waitFor();
  const contrast=await page.evaluate(()=>{
    const rgb=value=>value.match(/[\d.]+/g).slice(0,3).map(Number);
    const luminance=values=>values.map(n=>{const v=n/255;return v<=0.04045?v/12.92:((v+0.055)/1.055)**2.4})
      .reduce((sum,n,i)=>sum+n*[0.2126,0.7152,0.0722][i],0);
    const a=luminance(rgb(getComputedStyle(document.querySelector('#installation-warning')).color));
    const b=luminance(rgb(getComputedStyle(document.documentElement).backgroundColor));
    return (Math.max(a,b)+0.05)/(Math.min(a,b)+0.05);
  });
  assert.ok(contrast>=4.5,`dark warning contrast must remain readable: ${contrast.toFixed(2)}:1`);
  await page.screenshot({path:path.join(root,'artifacts/popup-dark.png'),fullPage:true});
  await page.evaluate(()=>{fixture.status.browserHealth.tabs=[{tabId:7,state:'reloadRequired'}];fixture.reloadError='Generated reload refusal'});
  await page.getByRole('button',{name:'Reload meeting tab',exact:true}).waitFor();
  await page.locator('#reload').click();
  await page.getByText('Generated reload refusal',{exact:true}).waitFor();
  await page.evaluate(()=>fixture.statusError='Generated worker failure');
  await page.getByRole('heading',{name:'Meeting detection is unavailable',exact:true}).waitFor();
  assert.equal(await page.locator('#reload').isVisible(),false);
  assert.equal(await page.locator('#connection').getAttribute('data-connected'),'false');
  assert.match(await page.locator('#recording-note').textContent(),/unavailable/);
  assert.equal(await page.locator('#action-error').isVisible(),false,'worker failure clears stale action refusal');
  await page.evaluate(()=>{delete fixture.statusError;fixture.status.browserHealth.tabs=[{tabId:7,state:'ready'}]});
  await page.getByRole('heading',{name:'Meeting sources detected',exact:true}).waitFor();
  await page.evaluate(()=>fixture.statusNeverResolves=true);
  await page.getByRole('heading',{name:'Meeting detection is unavailable',exact:true}).waitFor();
  assert.match(await page.locator('#explanation').textContent(),/did not answer in time/);
  assert.equal(await page.locator('#connection').getAttribute('data-connected'),'false');
  assert.equal(await page.evaluate(()=>fixture.requests.some(r=>r.type==='start'||r.type==='stop')),false,'the popup never uses the direct capture RPC');
  assert.deepEqual(errors,[]);
  await context.close();
  console.log('PASS popup: Start/Stop asks the app for the current tab with reasons and feedback, current-tab health/reload, refused reload, honest recovery, copy without meeting URLs, connection loss, light/dark rendering');
} finally {await browser?.close();await new Promise(resolve=>server.close(resolve));}

async function recordFlow(page){
  await page.evaluate(()=>{fixture.status.appRecording='disconnected';fixture.status.browserHealth.tabs=[{tabId:7,state:'ready'}]});
  await page.getByText('Open Cassini to record from the browser.',{exact:true}).waitFor();
  assert.equal(await page.locator('#record').isDisabled(),true);
  await page.evaluate(()=>{fixture.status.appRecording='available';fixture.status.browserHealth.tabs=[]});
  await page.getByText(/Join the call first/).waitFor();
  assert.equal(await page.locator('#record').isDisabled(),true);
  // A refusal from Cassini stays visible and the button stays usable.
  await page.evaluate(()=>{fixture.status.browserHealth.tabs=[{tabId:7,state:'ready'}];
    fixture.recordReply=()=>({error:'Generated app refusal',code:'needsSetup'})});
  const start=page.getByRole('button',{name:'Start recording',exact:true});
  await page.waitForFunction(()=>!document.querySelector('#record').disabled);
  await start.click();
  await page.getByText('Generated app refusal',{exact:true}).waitFor();
  await page.waitForFunction(()=>!document.querySelector('#record').disabled);
  assert.equal(await page.locator('#record-hint').isVisible(),false);
  // A click during an outstanding status poll: the refusal restores the card at
  // once, and the refresh it asks for runs when the held poll ends.
  await page.evaluate(()=>{delete window.releaseStatus;fixture.holdStatus=true});
  await page.waitForFunction(()=>typeof window.releaseStatus==='function');
  const statusCount=await page.evaluate(()=>fixture.requests.filter(r=>r.type==='status').length);
  await page.evaluate(()=>{fixture.recordReply=()=>({error:'Generated refusal during a poll',code:'needsSetup'})});
  await start.click();
  await page.getByText('Generated refusal during a poll',{exact:true}).waitFor();
  assert.equal(await page.locator('#record').textContent(),'Start recording');
  assert.equal(await page.locator('#record').isDisabled(),false);
  assert.equal(await page.locator('#status').textContent(),'Meeting sources detected');
  assert.equal(await page.locator('#card').getAttribute('data-tone'),'good');
  assert.equal(await page.evaluate(()=>fixture.requests.filter(r=>r.type==='status').length),statusCount,'the held poll is still outstanding');
  await page.evaluate(()=>window.releaseStatus());
  await page.waitForFunction(n=>fixture.requests.filter(r=>r.type==='status').length>n,statusCount);
  await page.getByRole('heading',{name:'Meeting sources detected',exact:true}).waitFor();
  assert.equal(await page.locator('#record-error').isVisible(),true,'the refusal stays until the offered action changes');
  // An accepted ask shows progress until this tab's capture reflects it.
  await page.evaluate(()=>{fixture.recordReply=message=>new Promise(resolve=>{window.finishRecord=()=>{
    fixture.captures=[{tabId:7,recording:false,starting:true,recordRequest:{action:'start',state:'accepted'}}];resolve({tabId:7})}})});
  await start.click();
  await page.getByRole('button',{name:'Starting…',exact:true}).waitFor();
  assert.equal(await page.locator('#record').isDisabled(),true);
  assert.equal(await page.locator('#status').textContent(),'Asking Cassini to start recording…');
  // The whole card renders the pending state, not just the heading and button.
  assert.equal(await page.locator('#card').getAttribute('data-tone'),'neutral');
  assert.match(await page.locator('#explanation').textContent(),/Waiting for it to confirm/);
  assert.equal(await page.locator('#recording-note').textContent(),'Recording is not yet confirmed');
  assert.equal(await page.locator('#reload').isVisible(),false);
  assert.equal(await page.locator('#record-error').isVisible(),false,'a new click clears the old refusal');
  await page.evaluate(()=>window.finishRecord());
  await page.getByRole('heading',{name:'Asking Cassini to start recording…',exact:true}).waitFor();
  await page.evaluate(()=>{fixture.captures=[{tabId:7,recording:true,tracks:2}]});
  const stop=page.getByRole('button',{name:'Stop recording',exact:true});
  await stop.waitFor();
  assert.equal(await page.locator('#record').getAttribute('data-action'),'stop');
  await mkdir(path.join(root,'artifacts'),{recursive:true});
  await page.screenshot({path:path.join(root,'artifacts/popup-recording.png'),fullPage:true});
  await page.evaluate(()=>{fixture.recordReply=()=>{fixture.captures=[{tabId:7,recording:true,tracks:2,stopping:true}];return {tabId:7}}});
  await stop.click();
  await page.getByRole('button',{name:'Saving…',exact:true}).waitFor();
  assert.deepEqual(await page.evaluate(()=>fixture.requests.filter(r=>r.type==='recordRequest').map(({action,tabId})=>({action,tabId}))),
    [{action:'start',tabId:7},{action:'start',tabId:7},{action:'start',tabId:7},{action:'stop',tabId:7}]);
  await page.evaluate(()=>{delete fixture.captures;delete fixture.recordReply;delete fixture.status.appRecording;fixture.status.browserHealth.tabs=[]});
  await page.getByRole('heading',{name:'No meeting sources detected',exact:true}).waitFor();
}
