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
  if (!['popup.html', 'popup.js', 'popup-state.mjs'].includes(file)) {response.writeHead(404).end();return;}
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
        if(message.type==='reloadMeetingTab'){
          if(fixture.reloadError)return {error:fixture.reloadError};
          fixture.status.browserHealth.tabs=[];
        }
        if(fixture.statusError)throw Error(fixture.statusError);
        if(message.type==='status' && fixture.statusNeverResolves)return new Promise(()=>{});
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
  assert.equal(await page.getByRole('button',{name:/record|stop/i}).count(),0);
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
  assert.equal(report.currentTabId,7);
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
  assert.equal(await page.evaluate(()=>fixture.requests.some(r=>r.type==='start'||r.type==='stop')),false);
  assert.deepEqual(errors,[]);
  await context.close();
  console.log('PASS popup: no recording controls/RPCs, current-tab health/reload, refused reload, honest recovery, copy without meeting URLs, connection loss, light/dark rendering');
} finally {await browser?.close();await new Promise(resolve=>server.close(resolve));}
