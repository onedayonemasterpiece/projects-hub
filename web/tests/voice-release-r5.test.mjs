import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {reduceVoiceToolProgress, speechStartsNewUserBubble} from '../src/voiceUiContract.js';
const source=fs.readFileSync(new URL('../src/App.tsx',import.meta.url),'utf8');

test('real App timing callback distinguishes admitted speech, queued end and output without inventing transcript',()=>{
  const calls=[];
  const setters=['setSpeechActive','setSpeechPending','setPlaybackProblem','setInterimInputTranscript','setInputTranscriptSeen'];
  const names=[...setters,'setToolProgress','reduceVoiceToolProgress','speechStartsNewUserBubble','reserveUserVoiceBubble','settleCurrentVoiceBubble','userTurnBoundaryPendingRef','userTranscriptIndex','assistantTranscriptIndex','turnHasInput','userTurnAwaitingFinalRef','activeIdentity','nativeSurface'];
  const refs=[{current:true},{current:2},{current:4},{current:true},{current:true}];
  const activeIdentity={current:true};
  const reserveUserVoiceBubble=()=>calls.push(['reserveUserVoiceBubble',true]);
  const settleCurrentVoiceBubble=()=>calls.push(['settleCurrentVoiceBubble',true]);
  let toolProgress={id:'slow-operation',elapsed_ms:2500};
  const values=[...setters.map(name=>value=>calls.push([name,value])),update=>{toolProgress=update(toolProgress);},reduceVoiceToolProgress,speechStartsNewUserBubble,reserveUserVoiceBubble,settleCurrentVoiceBubble,...refs,activeIdentity,false];
  const body=source.match(/const applyVoiceTiming = useCallback\(\(event: string\) => \{([\s\S]*?)\n  \}, \[[^\]]*\]\)/)?.[1];
  assert.ok(body,'must exercise the actual App callback');
  assert.match(source,/onTiming: applyVoiceTiming/,'browser uses the same timing callback');
  assert.match(source,/applyVoiceTiming\(event.stage\)/,'native hardware timing uses the same callback');
  const callback=new Function(...names,`return event=>{${body}}`)(...values);
  callback('speech_start');
  assert.equal(toolProgress,null,'new hardware speech clears the previous waiting indicator');
  assert.ok(calls.some(([name,value])=>name==='setSpeechActive'&&value===true));
  assert.ok(calls.some(([name,value])=>name==='settleCurrentVoiceBubble'&&value===true));
  assert.ok(calls.some(([name,value])=>name==='reserveUserVoiceBubble'&&value===true));
  assert.equal(refs[1].current,-1);
  assert.equal(refs[2].current,-1);
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

test('signed APK publication requires explicit source and audio evidence outside stable latest',()=>{
  const workflow=fs.readFileSync(new URL('../../.github/workflows/android-release.yml',import.meta.url),'utf8');
  assert.doesNotMatch(workflow,/^\s{2}push:/m);
  assert.match(workflow,/workflow_dispatch:[\s\S]*?source_sha:[\s\S]*?android_run_id:/);
  assert.match(workflow,/--target "\$BUILD_SHA"/);
  assert.match(workflow,/--notes-file dist\/release-notes\.md --latest/);
  assert.doesNotMatch(workflow,/--prerelease|--clobber/);
});

test('browser archive and Python source dependency identify the same published SDK RC',()=>{
  const pkg=JSON.parse(fs.readFileSync(new URL('../package.json',import.meta.url),'utf8'));
  const lock=JSON.parse(fs.readFileSync(new URL('../package-lock.json',import.meta.url),'utf8'));
  const dep=lock.packages['node_modules/@onedayonemasterpiece/live-interaction'];
  const python=fs.readFileSync(new URL('../../pyproject.toml',import.meta.url),'utf8');
  const android=JSON.parse(fs.readFileSync(new URL('../../android/live-interaction-candidate.json',import.meta.url),'utf8'));
  assert.equal(dep.version,pkg.liveFramework.version);
  assert.equal(android.artifact_type,'npm_tgz');
  assert.equal(dep.resolved,android.archive_url);
  assert.equal(pkg.dependencies['@onedayonemasterpiece/live-interaction'],android.archive_url);
  assert.equal(lock.packages[''].dependencies['@onedayonemasterpiece/live-interaction'],android.archive_url);
  assert.equal(pkg.liveFramework.release_commit,android.release_commit);
  assert.equal(pkg.liveFramework.ref,android.ref);
  assert.match(dep.integrity,/^sha512-/);
  assert.ok(python.includes('live-interaction.git@'+pkg.liveFramework.ref));
});
