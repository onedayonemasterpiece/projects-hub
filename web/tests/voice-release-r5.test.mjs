import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {speechStartsNewUserBubble} from '../src/voiceUiContract.js';
const source=fs.readFileSync(new URL('../src/App.tsx',import.meta.url),'utf8');

test('real App timing callback distinguishes admitted speech, queued end and output without inventing transcript',()=>{
  const calls=[];
  const setters=['setSpeechActive','setSpeechPending','setPlaybackProblem','setInterimInputTranscript','setInputTranscriptSeen'];
  const names=[...setters,'speechStartsNewUserBubble','reserveUserVoiceBubble','settleCurrentVoiceBubble','userTurnBoundaryPendingRef','userTranscriptIndex','turnHasInput','userTurnAwaitingFinalRef','activeIdentity'];
  const refs=[{current:true},{current:2},{current:true},{current:true}];
  const activeIdentity={current:true};
  const reserveUserVoiceBubble=()=>calls.push(['reserveUserVoiceBubble',true]);
  const settleCurrentVoiceBubble=()=>calls.push(['settleCurrentVoiceBubble',true]);
  const values=[...setters.map(name=>value=>calls.push([name,value])),speechStartsNewUserBubble,reserveUserVoiceBubble,settleCurrentVoiceBubble,...refs,activeIdentity];
  const body=source.match(/onTiming: event => \{([\s\S]*?)\n      \},\n      onState:/)?.[1];
  assert.ok(body,'must exercise the actual App callback');
  const callback=new Function(...names,`return event=>{${body}}`)(...values);
  callback('speech_start');
  assert.ok(calls.some(([name,value])=>name==='setSpeechActive'&&value===true));
  assert.ok(calls.some(([name,value])=>name==='settleCurrentVoiceBubble'&&value===true));
  assert.ok(calls.some(([name,value])=>name==='reserveUserVoiceBubble'&&value===true));
  assert.equal(refs[1].current,-1);
  calls.length=0;callback('speech_end');
  assert.deepEqual(calls,[['setSpeechActive',false],['setSpeechPending',true]]);
  calls.length=0;callback('first_output_audio');
  assert.deepEqual(calls,[['setSpeechPending',false]]);
  calls.length=0;callback('audio_scheduled');
  assert.deepEqual(calls,[['setPlaybackProblem',null]]);
  calls.length=0;callback('audio_queue');assert.deepEqual(calls,[]);
  activeIdentity.current=false;
  const before=refs.map(ref=>({...ref}));
  for(const event of ['speech_start','speech_end','first_output_audio','audio_scheduled','audio_queue']) callback(event);
  assert.deepEqual(calls,[],'old identity must not update UI or reserve/settle a turn');
  assert.deepEqual(refs,before,'old identity must not modify capture/transcript refs');
});

test('live feedback is mounted before the first user transcript',()=>{
  assert.match(source,/\(voiceActive \|\| chatMessages.length > 0 \|\| interimInputTranscript \|\| playbackProblem\)/);
  assert.doesNotMatch(source,/Слышу сейчас|Текст появляется по мере распознавания|текст ещё не получен/);
  assert.match(source,/Жду ответ Миры…/);
  assert.match(source,/className="chat-status" role="alert">\{playbackProblem\}/);
});

test('finish button seals through shared API and is never a second Stop control',()=>{
  const control=source.match(/\{speechActive && networkOnline && \(([\s\S]*?)Готово, отвечай/)?.[1];
  assert.ok(control);
  assert.match(control,/clientRef.current\?\.finishTurn\(\)/);
  assert.doesNotMatch(control,/\.stop\(|disableMicrophone|sendText|\.input\(/);
  const types=fs.readFileSync(new URL('../src/live-interaction.d.ts',import.meta.url),'utf8');
  assert.match(types,/finishTurn\(\): boolean/);
});

test('product version changes always trigger the signed APK workflow with an exact source target',()=>{
  const workflow=fs.readFileSync(new URL('../../.github/workflows/android-release.yml',import.meta.url),'utf8');
  assert.match(workflow,/paths:[\s\S]*?"src\/projects_hub\/version.py"[\s\S]*?workflow_dispatch:/);
  assert.match(workflow,/--target "\$GITHUB_SHA"/);
});

test('declared browser and Python shared dependency match the merged release pin',()=>{
  const pkg=JSON.parse(fs.readFileSync(new URL('../package.json',import.meta.url),'utf8'));
  const lock=JSON.parse(fs.readFileSync(new URL('../package-lock.json',import.meta.url),'utf8'));
  const dep=lock.packages['node_modules/@onedayonemasterpiece/live-interaction'];
  const python=fs.readFileSync(new URL('../../pyproject.toml',import.meta.url),'utf8');
  assert.equal(dep.version,pkg.liveFramework.version);
  assert.ok(dep.resolved.endsWith('#'+pkg.liveFramework.ref));
  assert.ok(python.includes('live-interaction.git@'+pkg.liveFramework.ref));
});