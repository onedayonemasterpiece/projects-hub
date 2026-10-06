import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../src/App.tsx',import.meta.url),'utf8');
const body=source.match(/const refreshPendingSources = useCallback\(async \(\) => \{([\s\S]*?)\n  \}, \[boot\]\);/)?.[1];
const effect=source.match(/useEffect\(\(\) => \{\n    if \(!boot \|\| !conversation\) return;[\s\S]*?\}, \[boot, conversation\?\.id, refreshPendingSources\]\);/)?.[0];
assert.ok(body,'exercise the actual App queue callback');
assert.ok(effect,'conversation restoration must schedule a refresh');
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
function setup(read=async()=>[{id:'savedA',conversation_id:'CA'},{id:'savedB',conversation_id:'CB'}]) {
  const boot={actor:{id:'A'},workspace:{id:'W'}};
  const conversationRef={current:null},activeIdentity={current:true},themeRef={current:{actor:'A'}};
  let queue=[], reads=0;
  const refresh=new AsyncFunction('boot','conversationRef','activeIdentity','themeRef','listPendingVoiceSources','setPendingSources',body)
    .bind(null,boot,conversationRef,activeIdentity,themeRef,async workspace=>{assert.equal(workspace,'W');reads++;return read();},value=>{queue=value;});
  let previous=[];
  const useEffect=(callback,deps)=>{
    if(deps.some((value,index)=>!Object.is(value,previous[index]))) {previous=deps;callback();}
  };
  const render=new Function('boot','conversation','refreshPendingSources','useEffect',effect)
    .bind(null,boot);
  return {boot,conversationRef,activeIdentity,themeRef,refresh,
    render:conversation=>render(conversation,refresh,useEffect),get queue(){return queue;},get reads(){return reads;}};
}

test('slow authorized conversation restoration refreshes fast IndexedDB without displaying another actor',async()=>{
  const state=setup();
  let restore;
  const server=new Promise(resolve=>{restore=resolve;});
  const restoring=server.then(conversation=>{state.conversationRef.current=conversation;state.render(conversation);});
  state.render(null);
  await state.refresh(); // bootstrap recovery wins the race against the server
  assert.deepEqual(state.queue,[]);
  assert.equal(state.reads,0);
  restore({id:'CA',actor_id:'A',workspace_id:'W'});
  await restoring;await new Promise(resolve=>setTimeout(resolve,0));
  assert.deepEqual(state.queue,[{id:'savedA',conversation_id:'CA'}]);
  assert.equal(state.reads,1);
});

test('queue read completing after identity reset cannot publish private sources',async()=>{
  let finish;
  const state=setup(()=>new Promise(resolve=>{finish=resolve;}));
  state.conversationRef.current={id:'CA',actor_id:'A',workspace_id:'W'};
  const pending=state.refresh();
  state.activeIdentity.current=false;state.themeRef.current.actor='B';
  finish([{id:'savedA',conversation_id:'CA'}]);await pending;
  assert.deepEqual(state.queue,[]);
});

test('restored foreign actor or workspace cannot expose the local queue',async()=>{
  const state=setup();
  for(const conversation of [{id:'CB',actor_id:'B',workspace_id:'W'},{id:'CA',actor_id:'A',workspace_id:'OTHER'}]) {
    state.conversationRef.current=conversation;await state.refresh();
    assert.deepEqual(state.queue,[]);
  }
  assert.equal(state.reads,0);
});
