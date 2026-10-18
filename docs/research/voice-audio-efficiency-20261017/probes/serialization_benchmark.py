#!/usr/bin/env python3
"""Local no-network microbenchmark of existing candidate audio helpers.

No key/SDK/provider connection; no product source mutations or store writes.
Select the source checkouts with PYTHONPATH. AUDIO_PROBE_OUTPUT selects a new
JSON result path; default output is serialization-results.local.json in the cwd.
"""
from __future__ import annotations
import asyncio
import base64
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import struct
import sys
import time
import tracemalloc

from live_interaction.session_host import _QueueReader
from live_interaction.socket_transport import decode_audio, encode_output, INPUT_MAGIC
from live_interaction.provider import read_line, _guarded_send
from live_interaction.transcribe import input_payload
from projects_hub.live_transcription import CaptionSidecar

# Select checkouts via PYTHONPATH; hash the modules actually imported, not a
# particular scratch directory layout. No provider runner or resource SDK starts.
SOURCE_FILES = {
    name: Path(sys.modules[name].__file__).resolve()
    for name in (
        'live_interaction.socket_transport',
        'live_interaction.session_host',
        'live_interaction.provider',
        'live_interaction.transcribe',
        'projects_hub.live_transcription',
    )
}

class NoNetworkSink:
    def __init__(self):
        self.total_bytes = 0
        self.total_messages = 0
    async def send(self, payload):
        self.total_messages += 1
        self.total_bytes += len(payload)

async def forbidden_provider(**kwargs):
    raise AssertionError('No network provider may run in this benchmark')

def synthetic_pcm(size):
    return (bytes(range(256)) * ((size + 255) // 256))[:size]

async def measure(label, audio_bytes, operation, samples=5):
    # Keep calibration and every measured case substantially under one CPU minute.
    await operation(64)
    start = time.process_time_ns()
    await operation(256)
    per_iteration = max(1, (time.process_time_ns() - start) / 256)
    iterations = min(50000, max(256, int(120_000_000 / per_iteration)))
    measurements = []
    for _ in range(samples):
        gc.collect()
        wall0, cpu0 = time.perf_counter_ns(), time.process_time_ns()
        checksum = await operation(iterations)
        cpu, wall = time.process_time_ns()-cpu0, time.perf_counter_ns()-wall0
        measurements.append({'cpu_ns':cpu,'wall_ns':wall,'checksum':checksum})
    gc.collect()
    tracemalloc.start()
    await operation(256)
    retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    ns = statistics.median(item['cpu_ns'] / iterations for item in measurements)
    return {
        'case':label,'input_pcm_bytes':audio_bytes,'iterations_per_trial':iterations,'trials':samples,
        'cpu_ns_per_iteration_median':ns,
        'cpu_us_per_iteration_median':ns/1000,
        'cpu_ms_per_audio_second':ns/1e6*(32000/audio_bytes),
        'wall_ns_per_iteration_median':statistics.median(item['wall_ns']/iterations for item in measurements),
        'python_tracemalloc_peak_bytes_256_serial_iterations':peak,
        'python_tracemalloc_retained_bytes':retained,
        'raw':measurements,
    }

async def main():
    results=[]
    byte_examples=[]
    for size in (1280, 3200, 11000):
        pcm=synthetic_pcm(size)
        wire=struct.pack('!III', INPUT_MAGIC, 1, 20)+pcm
        b64=base64.b64encode(pcm).decode('ascii')
        queue_message={'type':'audio','data':b64,'audio_turn_generation':1,'queued_at':1792243200000}
        provider_message=input_payload(queue_message)
        byte_examples.append({'pcm_bytes':size,'audio_ms':size/32,'phone_wss_application_bytes':len(wire),
            'base64_ascii_bytes':len(b64),'main_ndjson_bytes':len((json.dumps(queue_message,ensure_ascii=False,separators=(',',':'))+'\n').encode()),
            'provider_json_bytes':len(json.dumps(provider_message).encode())})

        async def relay_roundtrip(n,wire=wire):
            total=0
            for _ in range(n):
                _,_,_,value=decode_audio(wire)
                encoded=base64.b64encode(value).decode('ascii')
                # Actual stdlib call in ProjectsHubLiveAdapter.input; disk path excluded.
                restored=base64.b64decode(encoded,validate=True)
                total+=len(restored)
            return total
        results.append(await measure('existing_wss_decode_base64_encode_and_adapter_decode',size,relay_roundtrip))

        async def main_queue(n,message=queue_message):
            reader=_QueueReader()
            sink=NoNetworkSink()
            for _ in range(n):
                reader.feed(message)
                parsed=await read_line(reader)
                # transcribe.input_payload(audio) is identical to main provider.py audio mapping.
                await _guarded_send(sink,input_payload(parsed),None)
            return sink.total_bytes
        results.append(await measure('existing_main_NDJSON_queue_parse_and_provider_JSON',size,main_queue))

        async def captions(n,b64=b64):
            sidecar=CaptionSidecar(environment={},binding='local-benchmark',vocabulary=[],emit=lambda _:None,provider_run=forbidden_provider)
            # A pending future activates the existing synchronous feed; no runner is started.
            sidecar.task=asyncio.get_running_loop().create_future()
            sink=NoNetworkSink()
            try:
                for _ in range(n):
                    assert sidecar.feed({'audio_base64':b64})
                    while sidecar.reader.pending_bytes:
                        parsed=await read_line(sidecar.reader)
                        await _guarded_send(sink,input_payload(parsed),None)
                sidecar._flush_audio(all_bytes=True)
                while sidecar.reader.pending_bytes:
                    parsed=await read_line(sidecar.reader)
                    await _guarded_send(sink,input_payload(parsed),None)
                return sink.total_bytes
            finally:
                sidecar.task.cancel()
                sidecar.reader.close()
        results.append(await measure('existing_caption_decode_rechunk_100ms_NDJSON_provider_JSON',size,captions))

    for size in (2400,4800):
        event={'type':'audio','seq':1,'mime_type':'audio/pcm;rate=24000','data':base64.b64encode(synthetic_pcm(size)).decode('ascii')}
        async def downlink(n,event=event):
            total=0
            for _ in range(n):
                total+=len(encode_output(event))
            return total
        result=await measure('existing_downlink_base64_decode_and_binary_frame',size,downlink)
        result['cpu_ms_per_audio_second']=result['cpu_ns_per_iteration_median']/1e6*(48000/size)
        result['sample_rate_hz']=24000
        results.append(result)
    cpu_line=next((x.split(':',1)[1].strip() for x in Path('/proc/cpuinfo').read_text().splitlines() if x.startswith('model name')),None)
    report={'method':{'network':False,'keys':False,'production':False,'store_disk_IO':False,
        'guard_sdk':False,'websocket_TLS_compression':False,'real_time_pacing':False,
        'warning':'Serial hot-loop microbenchmarks; not provider latency, browser/device battery, VPS capacity, concurrency or per-session RAM. Tracemalloc is a separate 256-iteration probe, not included in timing.'},
        'environment':{'python':sys.version,'platform':platform.platform(),'cpu_model':cpu_line,'visible_logical_cpus':os.cpu_count(),
           'process_peak_rss_kib_including_imports':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss},
        'declared_source_refs':{'framework':'3b852249','consumer_code':'4a4d658','consumer_head_docs':'ffa3b508'},
        'source_ref_note':'Declared refs identify the original research baseline; actual imported files/hashes are recorded below.',
        'source_files':{name:str(path) for name,path in SOURCE_FILES.items()},
        'source_sha256':{name:hashlib.sha256(path.read_bytes()).hexdigest() for name,path in SOURCE_FILES.items()},
        'byte_examples':byte_examples,'results':results}
    target=Path(os.environ.get('AUDIO_PROBE_OUTPUT', 'serialization-results.local.json')).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'report':str(target),'cases':[{k:v for k,v in r.items() if k in ('case','input_pcm_bytes','cpu_us_per_iteration_median','cpu_ms_per_audio_second','python_tracemalloc_peak_bytes_256_serial_iterations')} for r in results],'byte_examples':byte_examples},indent=2))

if __name__=='__main__':
    asyncio.run(main())
