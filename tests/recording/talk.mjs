import assert from 'node:assert/strict';
import {readFile, writeFile} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import path from 'node:path';

export async function run({context,worker,id,artifacts,poll}) {
  const second = await context.browser().newContext({permissions:['microphone','camera']});
  const meeting=await context.newPage(), remote=await second.newPage();
  try {
    for(const [page,name] of [[meeting,'Cassini Recorder'],[remote,'Cassini Remote']]) {
      await page.goto(process.env.TALK_CALL_URL);
      await poll(async()=>{
        if(await page.getByRole('button',{name:'Leave call',exact:true}).isVisible()) return true;
        const guest=page.getByPlaceholder('Guest',{exact:true});
        if(await guest.isVisible()) await guest.fill(name);
        const displayName=page.getByRole('textbox',{name:/Display name/});
        if(await displayName.isVisible()) await displayName.fill(name);
        const submit=page.getByRole('button',{name:'Submit name and join'});
        const join=page.getByRole('button',{name:/^(Start|Join) call$/}).last();
        for(const button of [submit,join]) {
          if(await button.isVisible() && await button.isEnabled()) {await button.click();break;}
        }
        return false;
      },`Talk participant ${name} joins`,30000);
      await page.getByRole('button',{name:'Leave call',exact:true}).waitFor();
      await page.screenshot({path:path.join(artifacts,`talk-${name.replaceAll(' ','-')}.png`)});
    }
    await meeting.bringToFront();
    await meeting.evaluate(()=>document.title='Cassini Talk Recorder Test');
    const windowId=await poll(()=>{
      try {return execFileSync('xdotool',['search','--onlyvisible','--name','Cassini Talk Recorder Test'],{encoding:'utf8',stdio:['ignore','pipe','ignore']}).trim().split('\n')[0];}
      catch {return false;}
    },'Talk window title reaches X11');
    execFileSync('xdotool',['windowfocus','--sync',windowId,'key','--clearmodifiers','ctrl+shift+9']);
    await poll(async()=>(await worker.evaluate(()=>chrome.runtime.getContexts({}))).some(c=>c.contextType==='POPUP'),'real Talk extension action');
    const popup=await context.newPage();
    await popup.goto(`chrome-extension://${id}/popup.html`);
    await meeting.bringToFront();
    async function command(type,mode) {
      const result=await popup.evaluate(({type,mode})=>chrome.runtime.sendMessage({target:'background',type,mode}),{type,mode});
      assert.ok(!result.error,JSON.stringify(result));return result;
    }
    const started=await command('start','tracks');
    await poll(async()=>{
      const manifest=JSON.parse(await readFile(path.join(started.path,'session.json')));
      if(!['local','remote'].every(kind=>manifest.tracks.some(t=>t.kind===kind))) return false;
      for(const track of manifest.tracks) {
        const bytes=await readFile(path.join(started.path,track.file));
        if((bytes.length-44)/(2*track.channels)<48000*4 || !bytes.subarray(44).some(b=>b!==0)) return false;
      }
      return true;
    },'Talk captures audible local/remote sources',15000);
    const during=await command('status');
    assert.equal(during.recording,true,JSON.stringify(during));
    assert.ok(during.tracks>=2,JSON.stringify(during));
    await command('stop');
    const stopped=await poll(async()=>{
      const state=await command('status');
      return !state.recording && state;
    },'Talk Stop finalizes sources',10000);
    assert.equal(stopped.recording,false);
    const manifest=JSON.parse(await readFile(path.join(stopped.path,'session.json')));
    assert.equal(manifest.state,'completed',JSON.stringify(manifest));
    assert.ok(manifest.tracks.some(t=>t.kind==='local'));
    assert.ok(manifest.tracks.some(t=>t.kind==='remote'));
    for(const track of manifest.tracks) {
      const bytes=await readFile(path.join(stopped.path,track.file));
      assert.equal(bytes.toString('ascii',0,4),'RIFF');
      assert.equal(bytes.readUInt32LE(40),bytes.length-44);
      assert.ok(track.frames>48000,`track ${track.kind}: enough received frames`);
      assert.ok(bytes.subarray(44).some(b=>b!==0),`track ${track.kind}: actual audible Talk audio`);
    }
    await writeFile(path.join(artifacts,'talk-results.json'),JSON.stringify({passed:true,session:stopped.path,manifest},null,2));
    await meeting.screenshot({path:path.join(artifacts,'talk-call.png')});
    console.log(`PASS real Nextcloud Talk two-guest call → extension → native host → ${manifest.tracks.length} audible source WAVs`);
    await popup.close();
  } finally {await second.close();await meeting.close();}
}
