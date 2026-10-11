import test from 'node:test';
import assert from 'node:assert/strict';
import {voiceStartupFailureNotice,voiceStartupFailureState,resolveTerminalVoiceState} from '../src/voiceUiContract.js';
test('resource admission is distinct from provider 503 and displays authority retry interval',()=>{
  const error=Object.assign(new Error('budget'),{status:503,code:'RESOURCE_TOKEN_BUDGET',retry_after_ms:60000});
  assert.equal(voiceStartupFailureState(error),'resource_denial');
  assert.match(voiceStartupFailureNotice(error),/ресурсным лимитом/);
  assert.match(voiceStartupFailureNotice(error),/60 с/);
  assert.doesNotMatch(voiceStartupFailureNotice(error),/модель.*недоступна/);
  assert.equal(resolveTerminalVoiceState('off',{reason:'resource_denial'}),'resource_denial');
});
test('provider outage stays provider failure and unknown retry is not invented',()=>{
  const provider=Object.assign(new Error('provider failed'),{status:503,code:'LIVE_PROVIDER_ERROR'});
  assert.equal(voiceStartupFailureState(provider),'provider_failure');
  assert.match(voiceStartupFailureNotice(provider),/503/);
  assert.equal(voiceStartupFailureNotice(Object.assign(new Error('unauthorized'),{status:401})),null);
  for(const retry of [undefined,null,NaN,Infinity,-1,'3000']){
    const error={code:'RESOURCE_TOKEN_BUDGET',retry_after_ms:retry};
    assert.match(voiceStartupFailureNotice(error),/позже/);
    assert.doesNotMatch(voiceStartupFailureNotice(error),/NaN|Infinity|через/);
  }
});
