// Exercise the shipped popup DOM and module in a disposable browser. Only the
// Chrome transport is a fixture; this test never connects to a native host.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile, mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const tokens = JSON.parse(await readFile(path.join(root,'ux/brand/tokens.json'),'utf8')).tokens;
const server = createServer(async (request, response) => {
  const file = request.url?.slice(1);
  if (!['popup.html', 'popup.js', 'popup-state.mjs', 'tab-evidence.mjs', 'participants.mjs', 'brand-tokens.css', 'brand/saturn-nav.svg'].includes(file)) {response.writeHead(404).end();return;}
  const types = {html: 'text/html', css: 'text/css', svg: 'image/svg+xml'};
  response.setHeader('Content-Type', types[file.split('.').pop()] || 'text/javascript');
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
      runtime:{connect(){const listeners=[];const port={onMessage:{addListener:fn=>listeners.push(fn)},onDisconnect:{addListener(){}}};
        (fixture.ports??=[]).push(port);fixture.push=()=>listeners.forEach(fn=>fn({type:'changed'}));return port},
       async sendMessage(message){
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
  // The real worker pushes a change notice whenever its state changes; the fixture stands in for it
  // after every scripted state change. The popup itself runs no timer.
  const evaluate=page.evaluate.bind(page);
  page.evaluate=async(...args)=>{const result=await evaluate(...args);await evaluate(()=>fixture.push?.());return result;};
  await page.goto(`http://127.0.0.1:${server.address().port}/popup.html`);
  await page.locator('main[aria-busy="false"]').waitFor();
  assert.equal(await page.locator('#status').textContent(),'No meeting sources detected');
  await brandLook(page,'light');
  assert.equal(await page.locator('#tab-title').textContent(),'Generated meeting <script>');
  // Without an opted-in app, Start is visible but disabled and says why.
  assert.equal(await page.locator('#record').isDisabled(),true);
  assert.match(await page.locator('#record-hint').textContent(),/cannot start recordings from the browser/);
  await recordFlow(page);
  await page.evaluate(()=>fixture.status.browserHealth.tabs=[{tabId:3,state:'reloadRequired'},{tabId:7,state:'ready'}]);
  await page.getByRole('heading',{name:'Meeting sources detected',exact:true}).waitFor();
  assert.equal(await page.locator('#reload').isVisible(),false);
  // Participant roster: display-only text for this tab, never markup, cleared when it goes away.
  assert.equal(await page.locator('#participants').isVisible(),false);
  await page.evaluate(()=>{fixture.status.tabEvidence={tabId:7,callEvidence:{state:'unknown',reason:'unverified'},captureCapability:{kind:'separated',assurance:'observed'}};
    fixture.status.participants=['Hypatia','<img src=x onerror=window.pwned=1>'];});
  assert.equal(await page.locator('#participants').isVisible(),false,'a change notice never re-inspects the meeting page');
  const statusRequests=()=>evaluate(()=>fixture.requests.filter(r=>r.type==='status'));
  const before=await statusRequests();
  assert.equal(before[0].fresh,true,'the open check is the first status request');
  assert.ok(before.slice(1).every(request=>request.fresh===undefined),'refreshes after open never ask for a page check');
  await page.locator('#recheck').click();
  await page.locator('#participants').waitFor({state:'visible'});
  assert.equal((await statusRequests()).filter(request=>request.fresh).length,2,'Check again asks for exactly one more page check');
  assert.deepEqual(await page.locator('#participant-list li').allTextContents(),['Hypatia','<img src=x onerror=window.pwned=1>']);
  assert.equal(await page.evaluate(()=>window.pwned),undefined,'names render as text, never markup');
  assert.doesNotMatch(await page.locator('#diagnostics').textContent(),/Hypatia/);
  await page.evaluate(()=>{delete fixture.status.participants;delete fixture.status.tabEvidence;});
  await page.locator('#participants').waitFor({state:'hidden'});
  // Whole-tab fallback: visible, explicit, handled by the browser alone; its Stop is direct as well.
  assert.equal(await page.locator('#record-tab').isVisible(),true);
  assert.match(await page.locator('#record-tab-hint').textContent(),/one stereo track, not separate participants/);
  await page.locator('#record-tab').click();
  await page.waitForFunction(()=>fixture.requests.some(r=>r.type==='start'));
  assert.deepEqual(await evaluate(()=>fixture.requests.find(r=>r.type==='start')),{target:'background',type:'start',mode:'tab',tabId:7});
  await page.evaluate(()=>{fixture.captures=[{tabId:7,recording:true,tracks:1,mode:'tab',captureId:'capture-A'}]});
  await page.waitForFunction(()=>document.querySelector('#record').textContent==='Stop recording'&&!document.querySelector('#record').disabled);
  assert.equal(await page.locator('#record-tab').isVisible(),false,'no fallback while capturing');
  const asked=()=>evaluate(()=>fixture.requests.filter(r=>r.type==='recordRequest'&&r.action==='stop').length);
  const askedBefore=await asked();
  await page.locator('#record').click();
  await page.waitForFunction(()=>fixture.requests.some(r=>r.type==='stop'));
  assert.equal(await asked(),askedBefore,'a whole-tab stop never asks the app');
  assert.deepEqual(await evaluate(()=>fixture.requests.find(r=>r.type==='stop')),{target:'background',type:'stop',tabId:7,captureId:'capture-A'},'Stop names the tab and capture it displayed');
  await page.evaluate(()=>{delete fixture.captures;fixture.requests=fixture.requests.filter(r=>r.type==='status')});
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
  for(let notice=1;notice<=2;notice++){ // two worker change notices, each answered by one refresh
    await evaluate(()=>fixture.push());
    await page.waitForFunction(([count,wanted])=>fixture.requests.filter(r=>r.type==='status').length>=count+wanted,[polls,notice]);
  }
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
  await brandLook(page,'dark');
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
  assert.equal(await page.locator('#record-tab').isVisible(),false,'no stale whole-tab button under a status error');
  assert.equal(await page.locator('#participants').isVisible(),false,'no stale roster under a status error');
  await page.evaluate(()=>{delete fixture.statusError;fixture.status.browserHealth.tabs=[{tabId:7,state:'ready'}]});
  await page.getByRole('heading',{name:'Meeting sources detected',exact:true}).waitFor();
  await page.evaluate(()=>fixture.statusNeverResolves=true);
  await page.getByRole('heading',{name:'Meeting detection is unavailable',exact:true}).waitFor();
  assert.match(await page.locator('#explanation').textContent(),/did not answer in time/);
  assert.equal(await page.locator('#connection').getAttribute('data-connected'),'false');
  assert.equal(await page.evaluate(()=>fixture.requests.some(r=>r.type==='start'||r.type==='stop')),false,'the popup never uses the direct capture RPC');
  // A failed opening check is spent: the next worker push is an ordinary refresh, never a second page inspection.
  const failed=await context.newPage();
  await failed.addInitScript(()=>{fixture.statusError='Generated opening failure'});
  await failed.goto(`http://127.0.0.1:${server.address().port}/popup.html`);
  await failed.getByRole('heading',{name:'Meeting detection is unavailable',exact:true}).waitFor();
  await failed.evaluate(()=>{delete fixture.statusError;fixture.push()});
  await failed.waitForFunction(()=>fixture.requests.filter(r=>r.type==='status').length>=2);
  assert.deepEqual(await failed.evaluate(()=>fixture.requests.filter(r=>r.type==='status').map(r=>r.fresh===true)),[true,false],
    'only the first request asked for a page check');
  await failed.close();
  assert.deepEqual(errors,[]);
  await context.close();
  console.log('PASS popup: Start/Stop asks the app for the current tab with reasons and feedback, current-tab health/reload, refused reload, honest recovery, copy without meeting URLs, connection loss, light/dark rendering');
} finally {await browser?.close();await new Promise(resolve=>server.close(resolve));}

// D-976: the Saturn mark and lowercase wordmark head the popup, and the drawn colours are the
// cassini-ux brand roles in the active mode (vendored ux/brand/tokens.json via brand-tokens.css).
async function brandLook(page,mode){
  assert.equal(await page.locator('header h1').textContent(),'cassini.talk');
  assert.ok(await page.locator('header img.mark').evaluate(img=>img.complete&&img.naturalWidth>0&&img.getAttribute('src')==='brand/saturn-nav.svg'),'the Saturn mark loads');
  assert.equal(Math.round(await page.locator('header img.mark').evaluate(img=>img.getBoundingClientRect().height)),26,'mark at 26');
  assert.equal(await page.locator('header h1').evaluate(e=>getComputedStyle(e).fontSize),'18px','wordmark at 18');
  assert.equal(await page.locator('header h1').evaluate(e=>getComputedStyle(e).fontWeight),'500','wordmark medium');
  const look=await page.evaluate(()=>{const css=selector=>getComputedStyle(document.querySelector(selector));
    return {ground:css('html').backgroundColor,ink:css('html').color,record:css('#record').backgroundColor,
      onRecord:css('#record').color,card:css('#card').backgroundColor,tone:document.querySelector('#card').dataset.tone};});
  assert.equal(look.ground,rgb(tokens.raised[mode]));
  assert.equal(look.ink,rgb(tokens.ink[mode]));
  assert.equal(look.record,rgb(tokens.record[mode]),'Start is the Record red');
  assert.equal(look.onRecord,rgb(tokens['on-record'][mode]));
  const card={neutral:'sunken',good:'success-tint',warning:'warn-tint'}[look.tone||'neutral'];
  assert.equal(look.card,rgb(tokens[card][mode]),`${look.tone} card is ${card}`);
}
function rgb(hex){const [r,g,b]=[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16));return `rgb(${r}, ${g}, ${b})`;}

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
  assert.equal(await page.locator('#record').evaluate(e=>getComputedStyle(e).backgroundColor),rgb(tokens.record.light),'Stop keeps the Record red');
  await page.screenshot({path:path.join(root,'artifacts/popup-recording.png'),fullPage:true});
  await page.evaluate(()=>{fixture.recordReply=()=>{fixture.captures=[{tabId:7,recording:true,tracks:2,stopping:true}];return {tabId:7}}});
  await stop.click();
  await page.getByRole('button',{name:'Saving…',exact:true}).waitFor();
  assert.deepEqual(await page.evaluate(()=>fixture.requests.filter(r=>r.type==='recordRequest').map(({action,tabId})=>({action,tabId}))),
    [{action:'start',tabId:7},{action:'start',tabId:7},{action:'start',tabId:7},{action:'stop',tabId:7}]);
  await page.evaluate(()=>{delete fixture.captures;delete fixture.recordReply;delete fixture.status.appRecording;fixture.status.browserHealth.tabs=[]});
  await page.getByRole('heading',{name:'No meeting sources detected',exact:true}).waitFor();
}
