// Actual Chromium rendering against the candidate PWA, with prepared API fixtures.
// This does not establish provider speech, physical microphone or audible confirmation.
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';

const server = spawn('npm', ['run','dev','--','--port','5179'], {stdio:'ignore'});
const browser = await chromium.launch({headless:true,args:['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream']});
try {
  for (let attempt=0;attempt<100;attempt++) {
    try { if ((await fetch('http://127.0.0.1:5179')).ok) break; } catch {}
    await new Promise(resolve=>setTimeout(resolve,100));
  }
  for (const viewport of [{width:1280,height:900},{width:390,height:844}]) {
    const context = await browser.newContext({viewport,reducedMotion:'reduce',permissions:['microphone']});
    const page = await context.newPage();
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    let actor='A', authenticated=true, socket=null, sessionStarts=0, seq=0;
    let saved = {theme:'light',revision:1};
    const acks=[];
    let delayedMemory=null, releaseMemory=null, delayedFailure=null, releaseFailure=null;
    let sourceKnown=false, sourceStatus='live', replayFrames=0, replayStops=0, replayEnded=null, replayInputDone=null;
    let replayMode=false, delayedConversation=null, releaseConversation=null;
    const boot=()=>({actor:{id:actor,display_name:actor},workspace:{id:'W',name:'Projects'},role:'member',projects:[{id:'P',name:'Projects Hub',status:'active'}],preferences:saved});
    const conversation=()=>({id:'C'+actor,actor_id:actor,workspace_id:'W',focus_project_id:null,focus_project_name:null});
    await page.addInitScript(()=>{
      window.fixtureCaptures=0;window.fixtureTracks=[];
      const original=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getUserMedia=async(...args)=>{
        const stream=await original(...args);window.fixtureCaptures++;
        window.fixtureTracks.push(...stream.getTracks());return stream;
      };
    });
    await page.routeWebSocket('**/api/live/**/socket', ws=>{
      socket=ws;
      ws.onMessage(raw=>{
        if(typeof raw!=='string') {
          const bytes=new Uint8Array(raw);
          const view=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength);
          ws.send(JSON.stringify({type:'audio_ack',seq:view.getUint32(4,false)}));
          if(replayMode) replayFrames++;
          return;
        }
        const message=JSON.parse(raw);
        if(replayMode && message.type==='stop') replayStops++;
        if(replayMode && (message.activity_end || message.message?.activity_end)) replayInputDone?.();
        if(message.type==='hello') ws.send(JSON.stringify({type:'hello_ack',protocol:'wl-live-v1',connection_generation:message.connection_generation}));
        if(message.type==='ping') ws.send(JSON.stringify({type:'pong'}));
      });
    });
    const emit=event=>socket.send(JSON.stringify({type:'event',event:{seq:++seq,session_id:'S'+sessionStarts,conversation_id:'C'+actor,...event}}));
    await context.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      let payload = {};
      if(path==='/api/dev/login') {
        authenticated=true;
        const body=route.request().postDataJSON();
        if(body.display_name==='B') {actor='B';saved={theme:'dark',revision:0};}
        payload=boot();
      } else if(path==='/api/auth/config') payload={mode:'loopback_dev'};
      else if(!authenticated) {await route.fulfill({status:401,json:{detail:'AUTH_REQUIRED'}});return;}
      else if(path==='/api/bootstrap') payload=boot();
      else if(path==='/api/preferences') payload={actor_id:actor,...saved};
      else if(path==='/api/conversations' && route.request().method()==='POST') payload=conversation();
      else if(path.includes('/sources/by-client/')) {
        if(!sourceKnown) {await route.fulfill({status:404,json:{detail:'NOT_FOUND'}});return;}
        payload={source:{id:'source_fixture',status:sourceStatus}};
      } else if(path==='/api/tasks' && delayedFailure) {
        delayedFailure();delayedFailure=null;await new Promise(resolve=>{releaseFailure=resolve;});
        await route.fulfill({status:401,json:{detail:'AUTH_REQUIRED'}});return;
      } else if(path.startsWith('/api/conversations/')) {
        if(path!=='/api/conversations/C'+actor) {await route.fulfill({status:404,json:{detail:'NOT_FOUND'}});return;}
        if(delayedConversation) {
          delayedConversation();delayedConversation=null;
          await new Promise(resolve=>{releaseConversation=resolve;});
        }
        payload=conversation();
      } else if(path.endsWith('/sessions')) {
        sessionStarts++;
        const body=route.request().postDataJSON();
        replayMode=body.audio_mode==='buffered';
        if(replayMode) sourceKnown=true;
        payload={session_id:'S'+sessionStarts,source_id:'source_fixture',model:'gemini-3.8-live',
          attempt_id:body.attempt_id,transport_protocol:'wl-live-v1',socket_ticket:'fixture_ticket_12345678901234567890',
          socket_url:`ws://127.0.0.1:5179/api/live/C${actor}/sessions/S${sessionStarts}/socket`};
      } else if(path.endsWith('/applied')) {
        const ack=route.request().postDataJSON();
        assert.equal(ack.theme,saved.theme);assert.equal(ack.revision,saved.revision);
        assert.equal(ack.web_status,'applied');assert.equal(ack.native_status,'not_required');
        acks.push(ack);payload={application_status:'applied'};
      } else if(path==='/api/memories') {
        const requestedActor=actor;
        if(delayedMemory) {delayedMemory();delayedMemory=null;await new Promise(resolve=>{releaseMemory=resolve;});}
        payload={items:[{id:'M'+requestedActor,title:'Private note '+requestedActor,kind:'note',semantic_notes:'Secret '+requestedActor}]};
      } else if(path.includes('event')||path.includes('task')) payload={items:[]};
      else if(path.includes('sources')) payload={sources:[]};
      else if(path.includes('github')) payload={configured:false,connected:false,repositories:[]};
      await route.fulfill({json:payload});
    });
    await page.goto('http://127.0.0.1:5179/');
    await page.waitForFunction(()=>document.documentElement.dataset.theme==='light');
    const controls = await page.locator('button').count();
    assert.ok(controls>0);
    // Keep a real mounted element identity and focus throughout ten presentation changes.
    await page.locator('button').first().focus();
    await page.evaluate(()=>window.fixtureButton=document.activeElement);
    await page.getByRole('button',{name:'Начать голосовой разговор',exact:true}).click();
    await page.waitForFunction(()=>{const button=document.querySelector('.voice-orb');return button && !button.disabled && button.getAttribute('aria-label')==='Остановить разговор';});
    assert.ok(socket);
    const captureCount=await page.evaluate(()=>window.fixtureCaptures);
    assert.ok(captureCount>0);
    await page.locator('button').first().focus();
    await page.evaluate(()=>window.fixtureButton=document.activeElement);
    emit({type:'input_transcript',text:'Private conversation A'});
    for(let i=2;i<=11;i++) {
      saved={theme:i%2?'dark':'light',revision:i};
      emit({type:'preferences_changed',version:1,actor_id:actor,command_id:'theme_'+i.toString(16).padStart(64,'0'),...saved});
      // Wait for the actual App HTTP ACK, not a separately constructed controller.
      for(let attempt=0;acks.length<i-1 && attempt<100;attempt++) await page.waitForTimeout(20);
      assert.equal(acks.length,i-1);
      assert.equal(await page.evaluate(()=>document.activeElement===window.fixtureButton && window.fixtureButton.isConnected),true);
      emit({type:'output_transcript',text:'Theme acknowledged'});
      emit({type:'turn_complete'});
    }
    assert.equal(sessionStarts,1);
    assert.equal(await page.evaluate(()=>window.fixtureCaptures),captureCount);
    assert.equal(await page.evaluate(()=>window.fixtureTracks.every(track=>track.readyState==='live')),true);
    assert.ok(await page.getByText('Private conversation A',{exact:true}).isVisible());

    // Hold an A read across a cookie/account change in another tab.
    await page.locator('.context-island').click();
    const memoryRequested=new Promise(resolve=>{delayedMemory=resolve;});
    await page.getByRole('button',{name:'Память',exact:true}).click();
    await memoryRequested;
    const failureRequested=new Promise(resolve=>{delayedFailure=resolve;});
    const oldFailure=page.evaluate(async()=>{
      const api=await import('/src/api.ts');try {await api.getTasks('W');} catch {}
    });
    await failureRequested;
    const secondTab=await context.newPage();
    await secondTab.goto('http://127.0.0.1:5179/');
    await secondTab.evaluate(()=>fetch('/api/dev/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({display_name:'B'})}));
    await page.bringToFront();
    await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
    await page.waitForFunction(()=>document.documentElement.dataset.theme==='dark');
    await page.getByRole('button',{name:'Начать голосовой разговор',exact:true}).waitFor();
    releaseMemory();releaseFailure();await oldFailure;
    await page.waitForTimeout(200);
    await page.getByRole('button',{name:'Начать голосовой разговор',exact:true}).waitFor();
    assert.equal(await page.getByText('Private conversation A',{exact:true}).count(),0);
    assert.equal(await page.getByText('Private note A',{exact:true}).count(),0);
    assert.equal(await page.evaluate(()=>window.fixtureTracks.every(track=>track.readyState==='ended')),true);
    await secondTab.close();

    // Authentication expiry disposes B before a fresh login of the same actor.
    await page.getByRole('button',{name:'Начать голосовой разговор',exact:true}).click();
    await page.waitForFunction(()=>{const button=document.querySelector('.voice-orb');return button && !button.disabled && button.getAttribute('aria-label')==='Остановить разговор';});
    emit({type:'input_transcript',text:'Private conversation B'});
    await page.getByText('Private conversation B',{exact:true}).waitFor();
    const localId=await page.evaluate(async()=>{
      const storage=await import('/src/offlineSources.ts');
      const source=await storage.createLocalVoiceSource('W','CB');
      const sink=storage.createLocalPersistSink(source.id);
      await sink.persist({pcm:new Int16Array(320),sample_rate:16000,captured_at_ms:Date.now()});
      await sink.drain();await storage.sealLocalVoiceSource(source.id);
      return source.id;
    });
    const loginDocument=await page.evaluate(()=>performance.timeOrigin);
    authenticated=false;
    await page.evaluate(()=>window.dispatchEvent(new Event('projects-hub-theme-resume')));
    await page.getByRole('button',{name:'Войти в пилот',exact:true}).waitFor();
    assert.equal(await page.getByText('Private conversation B',{exact:true}).count(),0);
    const loginConversationRequested=new Promise(resolve=>{delayedConversation=resolve;});
    await page.getByRole('button',{name:'Войти в пилот',exact:true}).click();
    await loginConversationRequested;
    await page.waitForTimeout(150);
    assert.equal(await page.getByRole('button',{name:'Передать запись',exact:true}).count(),0);
    releaseConversation();
    await page.getByRole('button',{name:'Передать запись',exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>performance.timeOrigin),loginDocument);
    await page.getByRole('button',{name:'Начать голосовой разговор',exact:true}).waitFor();
    assert.equal(await page.getByText('Private conversation B',{exact:true}).count(),0);
    // Deliberate durable replay through App stays open across both handoffs.
    // IndexedDB is ready while server restoration is deliberately delayed.
    const conversationRequested=new Promise(resolve=>{delayedConversation=resolve;});
    await page.reload({waitUntil:'domcontentloaded'});
    await conversationRequested;
    await page.waitForTimeout(150);
    releaseConversation();
    await page.getByRole('button',{name:'Передать запись',exact:true}).waitFor();
    replayEnded=new Promise(resolve=>{replayInputDone=resolve;});
    await page.getByRole('button',{name:'Передать запись',exact:true}).click();
    await replayEnded;
    emit({type:'capability_transition_requested',transition_id:'pref'});
    emit({type:'turn_complete'});
    await page.waitForTimeout(200);
    assert.equal(replayStops,0);
    assert.equal(replayFrames,1);
    assert.ok(await page.evaluate(async id=>(await import('/src/offlineSources.ts')).getLocalVoiceSource(id),localId));
    emit({type:'capability_ready',transition_id:'pref'});
    saved={theme:'light',revision:1};
    const beforeReplayAck=acks.length;
    emit({type:'preferences_changed',version:1,actor_id:actor,command_id:'theme_'+'f'.repeat(64),...saved});
    for(let attempt=0;acks.length===beforeReplayAck && attempt<100;attempt++) await page.waitForTimeout(20);
    assert.equal(acks.length,beforeReplayAck+1);
    emit({type:'capability_transition_requested',transition_id:'memory'});
    emit({type:'turn_complete'});
    await page.waitForTimeout(200);assert.equal(replayStops,0);
    emit({type:'capability_ready',transition_id:'memory'});
    sourceStatus='ephemeral_processed';
    await page.waitForTimeout(200);assert.equal(replayStops,0);
    emit({type:'turn_complete'});
    await page.waitForFunction(async id=>(await import('/src/offlineSources.ts')).getLocalVoiceSource(id).then(value=>value===null),localId);
    for(let attempt=0;replayStops===0 && attempt<100;attempt++) await page.waitForTimeout(20);
    assert.equal(replayStops,1);assert.equal(replayFrames,1);
    // Reconciliation and reload use the actor's authoritative saved choice.
    saved={theme:'light',revision:11};
    await page.reload();
    await page.waitForFunction(()=>document.documentElement.dataset.theme==='light');
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(()=>document.activeElement?.tagName),'BUTTON');
    await page.locator('.context-island').click();
    await page.waitForSelector('.context-sheet');
    for(const theme of ['light','dark']) {
      await page.evaluate(async(theme)=>{const {renderTheme}=await import('/src/themePreferences.js');await renderTheme({theme,revision:12});},theme);
      const palette=await page.evaluate(()=>{
        const style=getComputedStyle(document.documentElement);
        return Object.fromEntries(['--canvas','--white','--muted','--muted-2','--focus','--positive','--warn'].map(key=>[key,style.getPropertyValue(key).trim()]));
      });
      const luminance=hex=>{const rgb=hex.slice(1).match(/../g).map(v=>parseInt(v,16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722;};
      const contrast=(a,b)=>{const x=luminance(a),y=luminance(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);};
      for(const token of ['--white','--muted','--muted-2','--positive','--warn']) assert.ok(contrast(palette[token],palette['--canvas'])>=4.5,`${theme} ${token}`);
      assert.ok(contrast(palette['--focus'],palette['--canvas'])>=3);
    }
    assert.deepEqual(errors,[]);
    await context.close();
    console.log(`CHROMIUM_THEME_PASS ${viewport.width}x${viewport.height} prepared API fixtures; no microphone/provider claim`);
  }
} finally { await browser.close();server.kill('SIGTERM'); }
