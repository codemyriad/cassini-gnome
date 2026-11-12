// Fixture wait: subscribe first, then read retained state. Events received while
// an asynchronous read is in flight cause another read, never a lost wakeup.
export function eventValue(subscribe, read, {label='fixture state', timeout=15000,
 later=setTimeout, cancel=clearTimeout}={}) {
 return new Promise((resolve,reject)=>{
  let stop=()=>{},timer,done=false,reading=false,pending=false;
  function finish(error,value){
   if(done)return;done=true;cancel(timer);stop();error?reject(error):resolve(value);
  }
  async function drain(){
   if(reading||done)return;reading=true;
   try {
    while(pending&&!done){pending=false;const value=await read();if(value)finish(null,value);}
   }catch(error){finish(error)}finally{reading=false}
  }
  function changed(){pending=true;void drain()}
  try {
   stop=subscribe(changed,error=>finish(error));
   if(done){stop();return}
   timer=later(()=>finish(Error('Timed out waiting for '+label)),timeout);
   changed();
  }catch(error){finish(error)}
 });
}
