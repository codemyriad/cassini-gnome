// Public guest-call flow adapted from bookmarklet-waveforms/tests/harness.spec.js.
// Drive Talk's real UI; do not simulate RTCPeerConnection or discovery messages.
export async function joinNextcloudTalk(page, {url, name, muted=false}) {
  await page.goto(url,{waitUntil:'domcontentloaded'});
  await page.locator('#app-content-vue').waitFor({state:'attached',timeout:30000});
  const leave=page.getByRole('button',{name:'Leave call',exact:true});
  const call=page.getByRole('button',{name:/^(Start|Join) call$/}).first();
  const submit=page.getByRole('button',{name:'Submit name and join',exact:true});
  // Talk renders its call button before the asynchronous guest welcome dialog.
  await submit.waitFor({state:'visible',timeout:5000}).catch(error=>{if(error.name!=='TimeoutError')throw error;});
  let joined=false;
  const deadline=Date.now()+45000;
  while(Date.now()<deadline){
    // Wait for a usable wizard footer or a joined call, not a settling delay.
    await page.waitForFunction(()=>{
      const visible=e=>!!(e&&e.getBoundingClientRect().width&&e.getBoundingClientRect().height);
      const dialogs=[...document.querySelectorAll('[role=dialog]')].filter(visible);
      const buttons=[...document.querySelectorAll('button')].filter(visible);
      const name=b=>b.getAttribute('aria-label')||b.innerText.trim();
      const leave=buttons.some(b=>name(b)==='Leave call');
      const dialog=dialogs.at(-1);
      if(!dialog)return leave||buttons.some(b=>/^(Start|Join) call$/.test(name(b)));
      return [...dialog.querySelectorAll('button')].filter(visible).some(b=>
        b.getAttribute('aria-label')==='Next'||b.classList.contains('join-call')||
        b.getAttribute('aria-label')==='Submit name and join'||b.textContent.trim()==='Submit name and join'||
        (leave&&b.getAttribute('aria-label')==='Close'));
    },null,{timeout:15000});
    const dialog=page.getByRole('dialog').filter({visible:true}).last();
    if(await dialog.count()){
      for(const label of ['Enter your name','Display name (required)']){
        const input=dialog.getByRole('textbox',{name:label,exact:true});
        if(await input.isVisible()&&await input.inputValue()!==name)await input.fill(name);
      }
      // Current Talk guest wizard exposes its name through the Guest placeholder.
      const guestName=dialog.getByPlaceholder('Guest',{exact:true});
      if(await guestName.isVisible()&&await guestName.inputValue()!==name)await guestName.fill(name);
      const audioToggle=dialog.getByRole('button',{name:muted?'Mute audio':'Unmute audio',exact:true});if(await audioToggle.isVisible())await audioToggle.click();
      const video=dialog.getByRole('button',{name:'Disable video',exact:true});if(await video.isVisible())await video.click();
      const join=dialog.locator('button.join-call');
      const next=dialog.getByRole('button',{name:'Next',exact:true});
      if(await submit.isVisible()){await submit.click();await submit.waitFor({state:'hidden'});continue;}
      if(await join.isVisible()){await join.click();await leave.waitFor({timeout:30000});continue;}
      if(await next.isVisible()){
        const before=await dialog.innerText();
        await next.click();
        await page.waitForFunction(text=>{
          const visible=[...document.querySelectorAll('[role=dialog]')].filter(e=>e.getBoundingClientRect().width&&e.getBoundingClientRect().height);
          return !visible.length||visible.at(-1).innerText!==text;
        },before,{timeout:15000});
        continue;
      }
      // Some Talk versions leave the device-settings modal open after joining;
      // its hidden wizard Next button must not be mistaken for an active step.
      const close=dialog.getByRole('button',{name:'Close',exact:true});
      if(await leave.isVisible()&&await close.isVisible()){
        await close.click({timeout:3000}).catch(async error=>{
          // The wizard can dismiss itself as the call connects during the click.
          if(await dialog.isVisible()||!await leave.isVisible())throw error;
        });
        continue;
      }
      // Talk can temporarily replace its footer while device settings settle.
      continue;
    }
    if(await leave.isVisible()){joined=true;break;}
    await call.click();
    await page.getByRole('dialog').filter({visible:true}).or(leave).first().waitFor({timeout:30000});
  }
  if(!joined)throw Error('Nextcloud guest wizard did not finish: '+(await page.getByRole('dialog').filter({visible:true}).last().innerText().catch(()=>'')));
  await leave.waitFor({timeout:30000});
  if(muted){
    const mute=page.getByRole('button',{name:/^Mute(?: audio)?(?: \(.*\))?$/i}).first();
    if(await mute.isVisible())await mute.click();
  }
  const disableVideo=page.getByRole('button',{name:/^Disable video(?: \(.*\))?$/i}).first();
  if(await disableVideo.isVisible())await disableVideo.click();
}

export async function talkMediaState(page) {
  return page.evaluate(()=>({
    version:globalThis.OC?.config?.versionstring || globalThis.OC?.config?.version || null,
    hook:globalThis.__cassiniPageHook || null,
    discovery:(globalThis.cassiniTalkDiscovery || []).slice(-5),
    // Janus can reuse the string ID 'janus0' across different receivers.
    // Count actual track objects, as the bookmarklet does, rather than IDs.
    remoteAudio:new Set([...document.querySelectorAll('audio,video')].filter(element=>
      !element.closest('.localVideoContainer, #localVideoContainer, [class*="local-video"], [class*="localVideo"]'))
      .flatMap(element=>element.srcObject?.getAudioTracks()||[]).filter(track=>track.readyState==='live'&&!track.muted)).size,
    media:[...document.querySelectorAll('audio,video')].map(element=>({
      tag:element.tagName,paused:element.paused,muted:element.muted,
      tracks:element.srcObject?.getTracks().map(track=>({id:track.id,kind:track.kind,ready:track.readyState,muted:track.muted}))||[]
    }))
  }));
}
