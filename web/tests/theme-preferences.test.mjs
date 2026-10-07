import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createThemePreferences, validPreference, renderTheme } from '../src/themePreferences.js';

const command = 'theme_' + 'a'.repeat(64);
const binding = (actor='A') => ({ version: 1, command_id: command, actor_id: actor, conversation_id: 'C', session_id: 'S' });
const setup = () => {
  const renders=[], acks=[], notices=[];
  const state = createThemePreferences({render: async value => { renders.push({...value}); return {web:true,native:'not_required'}; },
    acknowledge: async (request,value)=>acks.push({request,...value}), onStatus:value=>notices.push(value)});
  return {state,renders,acks,notices};
};

test('revision ordering, duplicate event and readback never roll back', async () => {
  const {state,renders,acks,notices}=setup();
  await state.reset('A');
  assert.equal(await state.apply({theme:'light',revision:1},binding()),true);
  assert.equal(await state.apply({theme:'dark',revision:2},binding()),true);
  assert.equal(await state.apply({theme:'light',revision:1}),false);
  assert.equal(await state.apply({theme:'dark',revision:2},binding()),true);
  assert.equal(await state.apply({theme:'light',revision:2}),false);
  assert.equal(notices.length,1);
  assert.deepEqual(state.preference,{theme:'dark',revision:2});
  assert.equal(acks.length,3); // duplicate ACK is harmless; no speech is manufactured
  assert.equal(renders.length,4);
});

test('old actor, invalid input and old Live generation cannot apply or ACK',async()=>{
  const {state,acks}=setup();
  await state.reset('A');
  for(const value of [{theme:'system',revision:1},{theme:'light',revision:true},{theme:'light',revision:-1},{theme:'light',revision:1.5}]) {
    assert.equal(validPreference(value),false);
    assert.equal(await state.apply(value,binding()),false);
  }
  assert.equal(await state.apply({theme:'light',revision:1},binding('B')),false);
  assert.equal(await state.apply({theme:'light',revision:1},binding(),()=>false),false);
  await state.reset('B');
  assert.equal(await state.apply({theme:'light',revision:1},binding('A')),false);
  assert.equal(acks.length,0);
  assert.deepEqual(state.preference,{theme:'dark',revision:0});
});

test('render opportunity is required and actor reset/new revision cancels a pending ACK',async()=>{
  const pending=[]; const acks=[];
  const state=createThemePreferences({render: (value,reset)=>reset?Promise.resolve({web:true}):new Promise(resolve=>pending.push(resolve)),
    acknowledge:async(value)=>acks.push(value)});
  await state.reset('A');
  const first=state.apply({theme:'light',revision:1},binding());
  assert.equal(acks.length,0);
  const second=state.apply({theme:'dark',revision:2},binding());
  pending.shift()({web:true});
  assert.equal(await first,false);
  await state.reset('B');
  pending.shift()({web:true});
  assert.equal(await second,false);
  assert.equal(acks.length,0);
});

test('missing native bridge is compatible; native failure never returns applied',async()=>{
  for(const native of ['unsupported','failed']) {
    const acks=[],notices=[];
    const state=createThemePreferences({render:async()=>({web:true,native}),acknowledge:async()=>acks.push(1),onStatus:m=>notices.push(m)});
    await state.reset('A');
    assert.equal(await state.apply({theme:'light',revision:1},binding()),native==='unsupported');
    assert.equal(acks.length,native==='unsupported'?1:0);
    assert.equal(notices.length,1);
  }
});


test('old Android ACK explicitly reports web applied and native unsupported', async () => {
  const acks=[];
  const state=createThemePreferences({render:async()=>({web:true,native:'unsupported'}),
    acknowledge:async(_,value)=>acks.push(value)});
  await state.reset('A');
  await state.apply({theme:'light',revision:1},binding());
  assert.deepEqual(acks,[{theme:'light',revision:1,web_status:'applied',native_status:'unsupported'}]);
});

test('overlapping native reconciliation owns its timer and preserves the later ACK', async () => {
  const saved = new Map(['document','window','location','getComputedStyle','requestAnimationFrame'].map(k=>[k,globalThis[k]]));
  const messages=[];
  const root={dataset:{},style:{}};
  const bridge={onmessage:null,postMessage:raw=>messages.push(JSON.parse(raw))};
  globalThis.document={documentElement:root,querySelector:()=>null};
  globalThis.window={projectsHubTheme:bridge};
  globalThis.location={search:'?native_version=test'};
  globalThis.getComputedStyle=()=>({getPropertyValue:()=>root.dataset.theme});
  globalThis.requestAnimationFrame=callback=>queueMicrotask(callback);
  try {
    const first=renderTheme({theme:'light',revision:1});
    await new Promise(resolve=>setTimeout(resolve,400));
    const second=renderTheme({theme:'light',revision:1});
    const dispatcher=bridge.onmessage;
    assert.equal((await first).native,'failed');
    assert.equal(bridge.onmessage,dispatcher);
    bridge.onmessage({data:JSON.stringify({...messages[0],applied:true})}); // expired ACK
    bridge.onmessage({data:JSON.stringify({...messages[1],applied:true})});
    assert.deepEqual(await second,{web:true,native:'applied'});
    assert.equal(bridge.onmessage,dispatcher);
  } finally {
    for(const [key,value] of saved) {
      if(value===undefined) delete globalThis[key]; else globalThis[key]=value;
    }
  }
});


test('old Android user agent without a version query reports native unsupported', async () => {
  const keys=['document','window','location','getComputedStyle','requestAnimationFrame','navigator'];
  const saved=new Map(keys.map(key=>[key,Object.getOwnPropertyDescriptor(globalThis,key)]));
  const root={dataset:{},style:{}};
  const values={document:{documentElement:root,querySelector:()=>null},window:{},location:{search:''},
    getComputedStyle:()=>({getPropertyValue:()=>root.dataset.theme}),
    requestAnimationFrame:callback=>queueMicrotask(callback),navigator:{userAgent:'ProjectsHubAndroid/old'}};
  try {
    for(const [key,value] of Object.entries(values)) Object.defineProperty(globalThis,key,{configurable:true,writable:true,value});
    assert.deepEqual(await renderTheme({theme:'light',revision:1}),{web:true,native:'unsupported'});
  } finally {
    for(const [key,descriptor] of saved) {
      if(descriptor) Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];
    }
  }
});
