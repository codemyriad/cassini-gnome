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


test('idle discovery forwards only evidence and reconnect requires a fresh hook answer',()=>{
 const ports=[],page=[],runtime=event(),handlers=new Map();
 const window={postMessage:m=>page.push(m),addEventListener:(name,fn)=>handlers.set(name,fn)};
 const chrome={runtime:{getURL:x=>'extension://'+x,onMessage:runtime,connect(){const port={sent:[],onMessage:event(),onDisconnect:event(),postMessage(m){this.sent.push(m)}};ports.push(port);return port}}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../relay.js'),'utf8'),{window,chrome});
 const send=data=>handlers.get('message')({source:window,data:{channel:'cassini-page',...data}});
 send({type:'discovery',sourceCount:3,heard:true,title:'Spoof',url:'https://spoof.invalid/',action:'start',path:'/spoof'});
 assert.deepEqual(JSON.parse(JSON.stringify(ports[0].sent)),[{type:'discovery',sourceCount:3,heard:true}]);
 for(const sourceCount of [-1,65,1.5,'2',true])send({type:'discovery',sourceCount});
 send({type:'command',requestId:'page',action:'start',tabId:2,mode:'tracks'});
 assert.equal(ports[0].sent.length,1);
 ports[0].onDisconnect.emit();runtime.emit({type:'cassini-connect'},{},()=>{});
 assert.deepEqual(JSON.parse(JSON.stringify(ports[1].sent)),[],'reconnect cannot replay a cached latch');
 send({type:'discovery',sourceCount:3,heard:false});
 assert.deepEqual(JSON.parse(JSON.stringify(ports[1].sent)),[{type:'discovery',sourceCount:3,heard:false}]);
 assert.ok(page.some(m=>m.type==='discover'));
 handlers.get('pagehide')();assert.deepEqual(JSON.parse(JSON.stringify(ports[1].sent.at(-1))),{type:'discovery',sourceCount:0,heard:false});
 const hiddenCount=ports[1].sent.length;
 send({type:'discovery',sourceCount:3,heard:true});assert.equal(ports[1].sent.length,hiddenCount,'late hidden-page proof is ignored');
 ports[1].onDisconnect.emit();handlers.get('pageshow')();
 assert.deepEqual(JSON.parse(JSON.stringify(ports[2].sent)),[],'pageshow must wait for its forced hook answer');
 send({type:'discovery',sourceCount:3,heard:false});
 assert.deepEqual(JSON.parse(JSON.stringify(ports[2].sent)),[{type:'discovery',sourceCount:3,heard:false}]);
 send({type:'discovery',sourceCount:3,heard:true});handlers.get('pagehide')();
 const shownCount=ports[2].sent.length;handlers.get('pageshow')();
 assert.equal(ports[2].sent.length,shownCount,'pageshow on the same port must not replay a cached count');
 send({type:'discovery',sourceCount:3,heard:false});assert.equal(ports[2].sent.at(-1).heard,false);
});
