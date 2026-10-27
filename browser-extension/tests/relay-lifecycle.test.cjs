const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const path=require('node:path');const vm=require('node:vm');
function event(){const list=[];return {addListener(fn){list.push(fn)},emit(...values){for(const fn of list)fn(...values)}}}
test('worker disconnect drains old page requests before forwarding a new session start',()=>{
 const ports=[],page=[],runtime=event();let handler;
 const window={postMessage:m=>page.push(m),addEventListener:(_type,fn)=>{handler=fn}};
 const chrome={runtime:{getURL:x=>'extension://'+x,onMessage:runtime,connect(){const sent=[];const port={sent,onMessage:event(),onDisconnect:event(),postMessage:m=>sent.push(m)};ports.push(port);return port}}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../relay.js'),'utf8'),{window,chrome});
 ports[0].onMessage.emit({type:'start',token:'old'});
 handler({source:window,data:{channel:'cassini-page',token:'old',type:'packet',request:1,packet:{type:'audio'}}});
 ports[0].onDisconnect.emit();assert.ok(page.some(m=>m.type==='ack'&&m.request===1&&m.error));
 handler({source:window,data:{channel:'cassini-page',token:'old',type:'packet',request:2,packet:{type:'audio'}}});
 assert.ok(page.some(m=>m.type==='ack'&&m.request===2&&m.error),'late orphaned requests must also drain');
 runtime.emit({type:'cassini-connect'},{},()=>{});ports[1].onMessage.emit({type:'start',token:'new'});
 assert.equal(page.filter(m=>m.type==='start').length,1);
 handler({source:window,data:{channel:'cassini-page',token:'old',type:'stopped'}});
 assert.equal(page.filter(m=>m.type==='start').length,2);assert.equal(page.at(-1).token,'new');
 assert.equal(ports[1].sent.length,0,'old stop cannot stop the new session');
 handler({source:window,data:{channel:'cassini-page',token:'new',type:'packet',request:3,packet:{type:'audio'}}});
 handler({source:window,data:{channel:'cassini-page',token:'new',type:'stopped'}});
 ports[1].onMessage.emit({type:'start',token:'third'});ports[1].onDisconnect.emit();
 assert.ok(!page.some(m=>m.type==='ack'&&m.token==='third'&&m.request===3),'normal stop clears old request ids before the next session');
});
