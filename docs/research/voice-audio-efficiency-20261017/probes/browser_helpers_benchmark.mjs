import fs from 'node:fs';
import os from 'node:os';
import {createHash} from 'node:crypto';
import {performance} from 'node:perf_hooks';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
// Source root is explicit; this probe never fetches code or starts a browser.
if(!process.env.FRAMEWORK_ROOT)throw new Error('Set FRAMEWORK_ROOT to the live-interaction checkout.');
const frameworkRoot=path.resolve(process.env.FRAMEWORK_ROOT);
const sourceUrl=relative=>pathToFileURL(path.join(frameworkRoot,relative));
const {createPcm16Resampler,frameRms}=await import(sourceUrl('browser/capture.js').href);
const {encodeLiveAudioFrame,decodeLiveOutputFrame}=await import(sourceUrl('browser/socket-transport.js').href);
const median=x=>[...x].sort((a,b)=>a-b)[Math.floor(x.length/2)];
function cpu(){const x=process.cpuUsage();return x.user+x.system;}
function measure(caseName,operation,audioSecondsPerIteration){
  operation(5000);
  let c=cpu();operation(10000);const perIteration=Math.max(.001,(cpu()-c)/10000);
  const iterations=Math.min(1000000,Math.max(1000,Math.floor(100000/perIteration)));
  const samples=[];
  for(let i=0;i<5;i++){
    global.gc?.();
    const c0=cpu(),w0=performance.now(),checksum=operation(iterations);
    samples.push({cpu_us:cpu()-c0,wall_ms:performance.now()-w0,checksum});
  }
  const us=median(samples.map(x=>x.cpu_us/iterations));
  return {case:caseName,iterations_per_trial:iterations,trials:5,
    cpu_us_per_iteration_median:us,cpu_ms_per_audio_second:us/1000/audioSecondsPerIteration,
    wall_us_per_iteration_median:median(samples.map(x=>x.wall_ms*1000/iterations)),raw:samples};
}
const samples=new Float32Array(128);for(let i=0;i<samples.length;i++)samples[i]=Math.sin(i*.11)*.2;
const results=[measure('existing_48k_to_16k_resample_and_rms_with_worklet_copy_128_samples',n=>{
  const r=createPcm16Resampler(48000);let total=0;
  for(let i=0;i<n;i++){
    const copied=new Float32Array(samples); // Exact capture-worklet allocation, without actual thread/message.
    const pcm=r.push(copied);total+=pcm.length+frameRms(copied);
  }
  return total;
},128/48000)];
for(const bytes of [1280,3200,11000]){
  const pcm=new Int16Array(bytes/2);pcm.fill(1234);
  results.push(measure(`existing_wss_encode_${bytes}_bytes`,n=>{let x=0;for(let i=0;i<n;i++)x+=encodeLiveAudioFrame(pcm,{seq:1,age_ms:20}).byteLength;return x;},bytes/32000));
}
const down=new Uint8Array(4812),v=new DataView(down.buffer);v.setUint32(0,0x574c4f31,false);v.setUint32(4,1,false);v.setUint32(8,24000,false);
results.push(measure('existing_wss_decode_4800_bytes',n=>{let x=0;for(let i=0;i<n;i++)x+=decodeLiveOutputFrame(down).pcm.byteLength;return x;},.1));
const sourceFiles=['browser/capture.js','browser/capture-worklet.js','browser/socket-transport.js'];
const source_sha256=Object.fromEntries(sourceFiles.map(path=>[path,createHash('sha256').update(fs.readFileSync(sourceUrl(path))).digest('hex')]));
const report={source_root:frameworkRoot,source_ref_note:'Declared ref identifies the original research baseline; actual file hashes are recorded separately.',declared_source_ref:'3b85224962c396e1a52977d6b81eee659db0cacc',source_sha256,method:{scope:'Imported existing pure helpers; no AudioWorklet thread, browser, real microphone, playback, crypto, network, disk or provider. Node is not an Android device or a VPS capacity measurement.',gc_exposed:Boolean(global.gc)},
 environment:{node:process.version,platform:process.platform,arch:process.arch,cpu_model:os.cpus()[0].model,visible_logical_cpus:os.cpus().length,memory_after_cases:process.memoryUsage()},results};
const target=path.resolve(process.env.AUDIO_PROBE_OUTPUT||'browser-helpers-results.local.json');
fs.mkdirSync(path.dirname(target),{recursive:true});
fs.writeFileSync(target,JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));
