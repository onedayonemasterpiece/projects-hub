"""Bounded asynchronous evidence refs in the existing voice source-event journal."""
from collections import OrderedDict
import asyncio
import hashlib
import json
import logging
import time

_LOG=logging.getLogger(__name__)


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


class KnowledgeContextJournal:
    def __init__(self,store):
        self.store=store;self.pending=OrderedDict();self.task=None;self.closed=False;self.bytes=0

    def retain(self,record):
        if self.closed:return False
        raw=canonical(record)
        key=(record['session_id'],record['turn_generation'],record['tool_call_id'])
        prior=self.pending.get(key)
        size=self.bytes-(len(prior[1]) if prior else 0)+len(raw)
        if len(raw)>65536 or size>512*1024 or key not in self.pending and len(self.pending)>=32:
            return False
        self.pending[key]=(record,raw);self.bytes=size
        if self.task is None or self.task.done():self.task=asyncio.create_task(self._run())
        return True

    def _commit(self,record,raw):
        started=time.monotonic()
        if not self.store._lock.acquire(timeout=.02):return False
        try:
            wait=(time.monotonic()-started)*1000
            self.store.get_conversation(record['actor_sub'],record['conversation_id'])
            self.store._membership(record['actor_sub'],record['workspace_id'])
            # A source/utterance ID is an exact existing committed turn, never
            # an approximate match to a nearby transcript or provider handle.
            known=self.store.db.execute('''SELECT 1 FROM source_events e JOIN sources s ON s.id=e.source_id
                WHERE e.source_id=? AND s.actor_id=? AND s.conversation_id=? AND e.kind='utterance_committed'
                AND json_extract(e.text,'$.utterance_id')=? LIMIT 1''',(record['source_id'],record['actor_sub'],
                    record['conversation_id'],record['utterance_id'])).fetchone()
            if known is None:return True # Orphan remains a context gap, not a new binding.
            digest=hashlib.sha256(raw).hexdigest()
            if not self.store.db.execute('SELECT 1 FROM source_events WHERE source_id=? AND kind=? AND payload_sha256=? LIMIT 1',
                    (record['source_id'],'knowledge_context_v2',digest)).fetchone():
                self.store.db.execute('''INSERT INTO source_events(source_id,kind,text,provider_at_ms,payload_sha256,created_at_ms)
                    VALUES(?,'knowledge_context_v2',?,NULL,?,?)''',(record['source_id'],raw.decode(),digest,round(time.time()*1000)))
            _LOG.info('regional knowledge context checkpoint',extra={'event':'knowledge_context_checkpoint',
                'request_id':record['request_id'],'disposition':record['disposition'],'lock_wait_ms':round(wait,3)})
            return True
        finally:self.store._lock.release()

    async def _run(self):
        while self.pending:
            key,(record,raw)=self.pending.popitem(last=False);self.bytes-=len(raw)
            try:
                committed=await asyncio.to_thread(self._commit,record,raw)
                if not committed:
                    # Optional context never blocks the mandatory utterance
                    # journal indefinitely; the gap is explicit after restart.
                    _LOG.info('regional knowledge context deferred',extra={'event':'knowledge_context_gap',
                        'request_id':record['request_id'],'reason':'checkpoint_lock_busy'})
            except Exception:
                _LOG.warning('regional knowledge context unavailable',extra={'event':'knowledge_context_gap',
                    'request_id':record['request_id'],'reason':'checkpoint_unavailable'})
            await asyncio.sleep(0)

    async def aclose(self):
        self.closed=True
        if self.task is not None:await self.task


def checkpoint(store,*,session,context,tool_call_id,utterance,query,result):
    if not utterance or not utterance.get('committed'):return False
    journal=getattr(store,'_knowledge_context_journal',None)
    if journal is None:
        journal=KnowledgeContextJournal(store);store._knowledge_context_journal=journal
    state=session.state
    return journal.retain(dict(codec='ph.rkb-evidence-context.v2',actor_sub=state['actor_id'],
        workspace_id=state['workspace_id'],conversation_id=state['conversation_id'],source_id=utterance['source_id'],
        utterance_id=utterance['id'],session_id=session.id,turn_generation=context.turn_generation,
        tool_call_id=tool_call_id,request_id=context.request_id,query=query,filters={'kinds':['source']},
        observed_data_seq=result['observed_data_seq'],observed_projection_seq=result['observed_projection_seq'],
        result_sha256=hashlib.sha256(canonical(result)).hexdigest(),
        evidence_refs=[{'id':item['id'],'metadata':item.get('metadata')} for item in result['evidence']],
        disposition='computed'))
