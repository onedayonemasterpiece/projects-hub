import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { canConnectVoiceSource, createVoiceSourceSetup } from '../src/voiceSourceSetup.js';

const required = { state: 'source_binding_required', can_connect_source: true,
  source: { repository: 'owner/notes', prefix: 'inbox/voice', access_mode: 'read_only' } };
const ready = { ...required, state: 'ready', can_connect_source: false };
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

test('only server-authorized exact read-only source exposes setup action', () => {
  assert.equal(canConnectVoiceSource(required), true);
  for (const setup of [null, ready, {...required,can_connect_source:false},
    {...required,state:'unavailable'}, {...required,source:null},
    {...required,source:{...required.source,prefix:'private/'}},
    {...required,source:{...required.source,access_mode:'write'}}]) {
    assert.equal(canConnectVoiceSource(setup), false);
  }
});

test('single click posts no targets or body and success waits for authoritative readback', async () => {
  const post = deferred(); const readback = deferred(); const calls = []; const states = [];
  const request = async (path, options) => {
    calls.push([path,options]);
    if (calls.length === 1) return required;
    if (options?.method === 'POST') return post.promise;
    return readback.promise;
  };
  const controller = createVoiceSourceSetup(request, state => states.push(state));
  await controller.load();
  const pending = controller.connect();
  await controller.connect();
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1], ['/api/voice-analysis/setup/connect-source',{method:'POST'}]);
  assert.equal(states.at(-1).busy, true);
  post.resolve(ready); await Promise.resolve(); await Promise.resolve();
  assert.equal(states.at(-1).connected, false);
  readback.resolve(ready); await pending;
  assert.equal(states.at(-1).connected, true);
  assert.equal(states.at(-1).busy, false);
  await controller.connect();
  assert.equal(calls.length, 3);
});

test('uncertain POST is reconciled by GET, never automatically repeated', async () => {
  let reads=0; let posts=0; let final;
  const controller=createVoiceSourceSetup(async (path,options) => {
    if(options?.method==='POST') { posts++; throw new Error('network'); }
    reads++; return reads===1 ? required : ready;
  },state=>{final=state;});
  await controller.load(); await controller.connect();
  assert.equal(posts,1); assert.equal(reads,2);
  assert.equal(final.connected,true); assert.equal(final.notice,null);
});

test('failure remains visible and permits a deliberate bounded retry', async () => {
  let posts=0; let final;
  const controller=createVoiceSourceSetup(async (path,options) => {
    if(options?.method==='POST') { posts++; throw new Error('private unsafe server detail'); }
    return required;
  },state=>{final=state;});
  await controller.load(); await controller.connect();
  assert.equal(final.connected,false); assert.equal(final.busy,false);
  assert.ok(final.notice); assert.ok(!final.notice.includes('private unsafe'));
  await controller.connect(); assert.equal(posts,2);
});

test('logout/unmount ignores late reads and never performs subsequent writes', async () => {
  const load=deferred(); let changes=0; let calls=0;
  const controller=createVoiceSourceSetup(async()=>{calls++;return load.promise;},()=>{changes++;});
  const pending=controller.load(); controller.dispose(); load.resolve(required); await pending;
  await controller.connect(); assert.equal(changes,0); assert.equal(calls,1);
});

test('owner component does not open legacy GitHub discovery and describes source-only success', () => {
  const component=fs.readFileSync(new URL('../src/VoiceSourceSetup.tsx',import.meta.url),'utf8');
  const app=fs.readFileSync(new URL('../src/App.tsx',import.meta.url),'utf8');
  assert.ok(component.includes('Автоматическая обработка включается отдельно'));
  assert.ok(component.includes('disabled={state.busy}'));
  assert.ok(!component.includes('getGitHubStatus') && !component.includes('refreshGitHub'));
  assert.ok(app.includes('boot.role === "owner" && <VoiceSourceSetup key={`${boot.actor.id}:${boot.workspace.id}`}'));
});
