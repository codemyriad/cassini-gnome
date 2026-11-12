// Diagnostic-only logical-clock adapter for the old spawn lifetime deadline.
// The actual Python child, framed bytes and VM relay are unchanged. Advance
// that deadline at the first real response, with no elapsed-time assertion.
const childProcess=require('node:child_process');
const spawn=childProcess.spawn;
childProcess.spawn=function(command,args,options){
 const {timeout,...liveOptions}=options;
 const child=spawn.call(this,command,args,liveOptions);
 if(timeout){
  child.stdout.once('data',()=>{
   process.stderr.write('Manual lifetime deadline expired during a live native response\n');
   child.kill('SIGTERM');
  });
 }
 return child;
};
