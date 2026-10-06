import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createThemePreferences, validPreference, renderTheme } from '../src/themePreferences.js';

const command = 'theme_' + 'a'.repeat(64);
const binding = (actor='A') => ({ version: 1, command_id: command, actor_id: actor });
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
