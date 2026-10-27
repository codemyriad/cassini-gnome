import assert from 'node:assert/strict';
import {readFile, writeFile} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import path from 'node:path';

export async function run({context,worker,id,artifacts}) {
  const second = await context.browser().newContext({permissions:['microphone','camera']});
  const meeting=await context.newPage(), remote=await second.newPage();
  try {
    for(const [page,name] of [[meeting,'Cassini Recorder'],[remote,'Cassini Remote']]) {
      await page.goto(process.env.TALK_CALL_URL);
      await page.waitForTimeout(1500);
      if (await page.getByRole('button',{name:'Submit name and join'}).isVisible()) {
        await page.getByPlaceholder('Guest',{exact:true}).fill(name);
        await page.getByRole('button',{name:'Submit name and join'}).click();
      }
      for(let step=0;step<4;step++) {
        await page.waitForTimeout(1200);
        if(await page.getByRole('button',{name:'Leave call',exact:true}).isVisible()) break;
        const guest=page.getByPlaceholder('Guest',{exact:true});
        if(await guest.isVisible()) await guest.fill(name);
        const displayName=page.getByRole('textbox',{name:/Display name/});
        if(await displayName.isVisible()) await displayName.fill(name);
        await page.getByRole('button',{name:/^(Start|Join) call$/}).last().click();
      }
      await page.getByRole('button',{name:'Leave call',exact:true}).waitFor();
      await page.screenshot({path:path.join(artifacts,`talk-${name.replaceAll(' ','-')}.png`)});
    }
    await meeting.waitForTimeout(3000);
    await meeting.bringToFront();
    await meeting.evaluate(()=>document.title='Cassini Talk Recorder Test');
    await meeting.waitForTimeout(300);
    execFileSync('xdotool',['search','--onlyvisible','--name','Cassini Talk Recorder Test','windowfocus','--sync','key','--clearmodifiers','ctrl+shift+9']);
    await meeting.waitForTimeout(500);
    const popup=await context.newPage();
    await popup.goto(`chrome-extension://${id}/popup.html`);
    await meeting.bringToFront();
    async function command(type,mode) {
      const result=await popup.evaluate(({type,mode})=>chrome.runtime.sendMessage({target:'background',type,mode}),{type,mode});
      assert.ok(!result.error,JSON.stringify(result));return result;
    }
    await command('start','tracks');
    await meeting.waitForTimeout(4000);
    const during=await command('status');
    assert.equal(during.recording,true,JSON.stringify(during));
    assert.ok(during.tracks>=2,JSON.stringify(during));
    await command('stop');
    let stopped;
    for(let n=0;n<100;n++) {
      stopped=await command('status');if(!stopped.recording)break;await meeting.waitForTimeout(100);
    }
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
