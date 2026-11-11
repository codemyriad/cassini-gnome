import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import test from 'node:test';
import {eventValue} from './support/event-value.mjs';

function fixture(){
 const events=new EventEmitter();let value;
 const subscribe=(notify)=>{events.on('change',notify);return()=>events.off('change',notify)};
 return {events,subscribe,read:()=>value,publish(next){value=next;events.emit('change')}};
}
function deferred(){let resolve;const promise=new Promise(r=>{resolve=r});return {promise,resolve}}

test('retained state published before subscription completes without a later event',async()=>{
 const f=fixture();f.publish({ready:true});
 assert.deepEqual(await eventValue(f.subscribe,f.read),{ready:true});
 assert.equal(f.events.listenerCount('change'),0);
});
test('publication after subscription completes the wait and removes its listener',async()=>{
 const f=fixture();const result=eventValue(f.subscribe,f.read);
 assert.equal(f.events.listenerCount('change'),1);f.publish('ready');
 assert.equal(await result,'ready');assert.equal(f.events.listenerCount('change'),0);
});
test('an event emitted during subscription is retained by the initial read',async()=>{
 const f=fixture();
 assert.equal(await eventValue(notify=>{const stop=f.subscribe(notify);f.publish('ready');return stop},f.read),'ready');
 assert.equal(f.events.listenerCount('change'),0);
});
test('an event during an asynchronous read schedules a new read without overlap',async()=>{
 const f=fixture(),first=deferred();let reads=0,active=0,peak=0;
 const result=eventValue(f.subscribe,async()=>{
  active++;peak=Math.max(peak,active);reads++;
  const value=reads===1?await first.promise:f.read();active--;return value;
 });
 assert.equal(reads,1);f.publish('new manifest');first.resolve(null);
 assert.equal(await result,'new manifest');assert.equal(reads,2);assert.equal(peak,1);
 assert.equal(f.events.listenerCount('change'),0);
});
test('a read error is preserved and unsubscribes',async()=>{
 const f=fixture(),error=new Error('storage read failed');
 await assert.rejects(eventValue(f.subscribe,()=>{throw error}),actual=>actual===error);
 assert.equal(f.events.listenerCount('change'),0);
});
test('subscription failure is preserved and its returned cleanup runs',async()=>{
 const error=new Error('watch failed');let closed=0;
 await assert.rejects(eventValue((notify,fail)=>{fail(error);return()=>closed++},()=>false),actual=>actual===error);
 assert.equal(closed,1);
});
test('the hang guard uses its registered timer and releases the subscription',async()=>{
 const f=fixture();let expired,cancelled;
 const result=eventValue(f.subscribe,f.read,{label:'manifest',timeout:123,
  later(fn,bound){assert.equal(bound,123);expired=fn;return 'timer'},cancel(timer){cancelled=timer}});
 const rejected=assert.rejects(result,/Timed out waiting for manifest/);
 expired();await rejected;
 assert.equal(cancelled,'timer');assert.equal(f.events.listenerCount('change'),0);
});
