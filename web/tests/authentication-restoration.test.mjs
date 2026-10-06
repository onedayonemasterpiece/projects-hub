import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../src/App.tsx',import.meta.url),'utf8');
const bootstrapBody=source.match(/const applyAuthenticatedBootstrap = useCallback\(async \([^\n]*\) => \{([\s\S]*?)\n  \}, \[hydratePreferences\]\);/)?.[1];
const signInBody=source.match(/async function signIn\(\) \{([\s\S]*?)\n  \}\n\n  async function ensureConversation/)?.[1];
const queueBody=source.match(/const refreshPendingSources = useCallback\(async \(\) => \{([\s\S]*?)\n  \}, \[boot\]\);/)?.[1];
assert.ok(bootstrapBody);assert.ok(signInBody);assert.ok(queueBody);
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
class ApiError extends Error {constructor(status){super('HTTP '+status);this.status=status;}}
function setup(getConversation,mode='loopback_dev') {
  const authenticated={actor:{id:'A'},workspace:{id:'W'},preferences:{theme:'dark',revision:0}};
  const activeIdentity={current:true},themeRef={current:{actor:null}},conversationRef={current:null};
  const storage=new Map([['projects-hub-conversation','CA']]);
  const localStorage={getItem:key=>storage.get(key)??null,removeItem:key=>storage.delete(key)};
  let boot=null, queue=[], reads=0, developmentLogins=0, inviteLogins=0;
  const refreshes=[];
  const setConversation=current=>{
    assert.equal(conversationRef.current,current);
    const refresh=new AsyncFunction('boot','activeIdentity','conversationRef','themeRef','listPendingVoiceSources','setPendingSources',queueBody);
    refreshes.push(refresh(boot,activeIdentity,conversationRef,themeRef,async()=>{reads++;return [
      {id:'savedA',conversation_id:'CA'},{id:'savedB',conversation_id:'CB'}];},value=>{queue=value;}));
  };
  const apply=new AsyncFunction('activeIdentity','hydratePreferences','themeRef','setBoot','localStorage','getConversation','conversationRef','setConversation','ApiError','value','isCurrent',bootstrapBody)
    .bind(null,activeIdentity,async value=>{themeRef.current.actor=value.actor.id;},themeRef,value=>{boot=value;},localStorage,getConversation,conversationRef,setConversation,ApiError);
  const signIn=new AsyncFunction('setBusy','setNotice','authConfig','inviteCode','exchangeInvite','applyAuthenticatedBootstrap','setInviteCode','login',signInBody)
    .bind(null,()=>{},()=>{},{mode},'valid-invite-code',async()=>{inviteLogins++;return authenticated;},value=>apply(value,()=>true),()=>{},async()=>{developmentLogins++;return authenticated;});
  return {signIn,activeIdentity,themeRef,conversationRef,storage,refreshes,
    get boot(){return boot;},get queue(){return queue;},get reads(){return reads;},
    get developmentLogins(){return developmentLogins;},get inviteLogins(){return inviteLogins;}};
}

test('login after 401 restores the saved authorized conversation and recording without reload',async()=>{
  let reply,requested;
  const requestStarted=new Promise(resolve=>{requested=resolve;});
  const response=new Promise(resolve=>{reply=resolve;});
  const state=setup(id=>{assert.equal(id,'CA');requested();return response;});
  const signingIn=state.signIn();await requestStarted;
  assert.equal(state.boot.actor.id,'A');assert.deepEqual(state.queue,[]);assert.equal(state.reads,0);
  reply({id:'CA',actor_id:'A',workspace_id:'W'});
  await signingIn;await Promise.all(state.refreshes);
  assert.equal(state.conversationRef.current.id,'CA');
  assert.deepEqual(state.queue,[{id:'savedA',conversation_id:'CA'}]);
  assert.equal(state.developmentLogins,1);
});

test('invite login shares the same authorized restoration path',async()=>{
  const state=setup(async()=>({id:'CA',actor_id:'A',workspace_id:'W'}),'first_party_invite');
  await state.signIn();await Promise.all(state.refreshes);
  assert.equal(state.inviteLogins,1);assert.equal(state.developmentLogins,0);
  assert.deepEqual(state.queue,[{id:'savedA',conversation_id:'CA'}]);
});

test('foreign conversation returned after login cannot expose another actor recording',async()=>{
  const state=setup(async()=>({id:'CA',actor_id:'B',workspace_id:'W'}));
  await state.signIn();
  assert.equal(state.conversationRef.current,null);assert.deepEqual(state.queue,[]);
  assert.equal(state.reads,0);assert.equal(state.storage.has('projects-hub-conversation'),false);
});

test('late login restoration cannot overwrite the next identity or its saved pointer',async()=>{
  let reply,requested;
  const requestStarted=new Promise(resolve=>{requested=resolve;});
  const state=setup(()=>{requested();return new Promise(resolve=>{reply=resolve;});});
  const signingIn=state.signIn();await requestStarted;
  state.activeIdentity.current=false;state.themeRef.current.actor='B';
  state.storage.set('projects-hub-conversation','CB');
  reply({id:'CA',actor_id:'A',workspace_id:'W'});await signingIn;
  assert.equal(state.conversationRef.current,null);assert.deepEqual(state.queue,[]);
  assert.equal(state.storage.get('projects-hub-conversation'),'CB');
});

test('401 during restoration retains the pointer for the next authorized login',async()=>{
  let fail,requested;
  const requestStarted=new Promise(resolve=>{requested=resolve;});
  const state=setup(()=>{requested();return new Promise((_,reject)=>{fail=reject;});});
  const signingIn=state.signIn();await requestStarted;
  state.activeIdentity.current=false;state.themeRef.current.actor=null;
  fail(new ApiError(401));await signingIn;
  assert.equal(state.storage.get('projects-hub-conversation'),'CA');
  assert.deepEqual(state.queue,[]);
});
