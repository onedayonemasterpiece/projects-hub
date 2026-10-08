import test from 'node:test';
import assert from 'node:assert/strict';
import {createReplayCompletion} from '../src/replayCompletion.js';

test('core to preferences to memory waits for the final turn and authoritative disposition',()=>{
  const completion=createReplayCompletion();
  completion.event({type:'turn_complete'});
  assert.equal(completion.confirmed('live',completion.epoch),false);
  const coreRead=completion.epoch;
  completion.event({type:'capability_transition_requested'});
  assert.equal(completion.canReadDisposition,false);
  completion.event({type:'turn_complete'});
  assert.equal(completion.confirmed('archived',completion.epoch),false);
  completion.event({type:'capability_ready'});
  assert.equal(completion.confirmed('archived',coreRead),false);
  assert.equal(completion.canReadDisposition,false);
  completion.event({type:'turn_complete'});
  const preferencesRead=completion.epoch;
  completion.event({type:'capability_transition_requested'});
  assert.equal(completion.confirmed('ephemeral_processed',preferencesRead),false);
  completion.event({type:'capability_ready'});
  assert.equal(completion.confirmed('ephemeral_processed',completion.epoch),false);
  completion.event({type:'turn_complete'});
  assert.equal(completion.confirmed('live',completion.epoch),false);
  assert.equal(completion.confirmed('ephemeral_processed',completion.epoch),true);
});
