// Actual Chromium rendering against the candidate PWA, with prepared API fixtures.
// This does not establish provider speech, physical microphone or audible confirmation.
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';

const server = spawn('npm', ['run','dev','--','--port','5179'], {stdio:'ignore'});
const browser = await chromium.launch({headless:true});
try {
  for (let attempt=0;attempt<100;attempt++) {
    try { if ((await fetch('http://127.0.0.1:5179')).ok) break; } catch {}
    await new Promise(resolve=>setTimeout(resolve,100));
  }
  for (const viewport of [{width:1280,height:900},{width:390,height:844}]) {
    const context = await browser.newContext({viewport,reducedMotion:'reduce'});
    const page = await context.newPage();
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    let saved = {theme:'light',revision:1};
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      let payload = {};
      if (path==='/api/auth/config') payload={mode:'loopback_dev'};
      else if (path==='/api/bootstrap') payload={actor:{id:'A',display_name:'Fixture'},workspace:{id:'W',name:'Projects'},role:'member',projects:[{id:'P',name:'Projects Hub',status:'active'}],preferences:saved};
      else if (path==='/api/preferences') payload=saved;
      else if (path.includes('event')) payload={event_cards:[]};
      else if (path.includes('task')) payload={tasks:[]};
      else if (path.includes('sources')) payload={sources:[]};
      else if (path.includes('memories')) payload={memories:[]};
      else if (path.includes('github')) payload={configured:false,connected:false,repositories:[]};
      await route.fulfill({json:payload});
    });
    await page.goto('http://127.0.0.1:5179/');
    await page.waitForFunction(()=>document.documentElement.dataset.theme==='light');
    const controls = await page.locator('button').count();
    assert.ok(controls>0);
    // Keep a real mounted element identity and focus throughout ten presentation changes.
    await page.locator('button').first().focus();
    await page.evaluate(()=>window.fixtureButton=document.activeElement);
    const rendered = await page.evaluate(async()=>{
      const {createThemePreferences,renderTheme} = await import('/src/themePreferences.js');
      const acks=[];
      const theme=createThemePreferences({render:renderTheme,acknowledge:async(binding,value)=>acks.push(value)});
      await theme.reset('A');
      for(let i=1;i<=10;i++) {
        const target=i%2?'light':'dark';
        if(!await theme.apply({theme:target,revision:i},{version:1,actor_id:'A',command_id:'theme_'+'a'.repeat(64)})) throw new Error('Theme not rendered');
        if(document.activeElement!==window.fixtureButton || !window.fixtureButton.isConnected) throw new Error('Focus/component replaced');
      }
      return {acks:acks.length,theme:document.documentElement.dataset.theme,scheme:getComputedStyle(document.documentElement).colorScheme};
    });
    assert.deepEqual(rendered,{acks:10,theme:'dark',scheme:'dark'});
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
