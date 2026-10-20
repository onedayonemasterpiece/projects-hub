"""Concrete bounded loopback RKB provider; service scope never grants human rights."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from .regional_knowledge import (KNOWLEDGE_CODEC, KnowledgeReadContext, RegionalKnowledgeAdapter,
    RegionalKnowledgeAccessError, RegionalKnowledgeContractError, RegionalKnowledgeUnavailable)

_LOG=logging.getLogger(__name__)


class PrivateFile:
    def __init__(self,path,maximum):
        self.path=Path(path)
        if not self.path.is_absolute():raise ValueError('private absolute runtime file required')
        fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or not 1<=info.st_size<=maximum:
                raise ValueError('private bounded runtime file required')
            self.data=os.read(fd,maximum+1)
            if len(self.data)!=info.st_size or os.read(fd,1):raise ValueError('runtime file changed')
            self.identity=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
        finally:os.close(fd)
    def current(self):
        try:
            info=self.path.lstat()
            return (stat.S_ISREG(info.st_mode) and not info.st_mode&0o077
                and (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)==self.identity)
        except OSError:return False


def failure(context,reason,status='unavailable'):
    return dict(codec=KNOWLEDGE_CODEC,request_id=context.request_id,turn_generation=context.turn_generation,
        status=status,reason=reason,mode=None,evidence=[],min_data_seq=context.min_data_seq,
        observed_data_seq=None,observed_projection_seq=None,prior_write_pending=False,
        context_gap=reason in {'context_gap','evidence_context_unavailable'},continuation=None)


@dataclass(frozen=True)
class Delegation:
    actor_sub:str
    workspace_id:str
    link_id:str
    link_revision:int

    def __post_init__(self):
        if (type(self.actor_sub) is not str or re.fullmatch('usr_[A-Za-z0-9_-]{1,250}',self.actor_sub) is None
                or type(self.workspace_id) is not str or re.fullmatch('ws_[A-Za-z0-9_-]{1,250}',self.workspace_id) is None
                or type(self.link_id) is not str or str(UUID(self.link_id))!=self.link_id
                or type(self.link_revision) is not int or not 1<=self.link_revision<2**63):
            raise ValueError('exact existing verified account binding required')


class RegionalKnowledgeHttpProvider:
    def __init__(self,*,endpoint,service_id,provider_tenant,token,binding,current,client=None):
        parsed=urlsplit(endpoint)
        if (parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path!='/internal/projects-hub/knowledge-search'
                or type(service_id) is not str or re.fullmatch('[A-Za-z0-9._:-]{1,128}',service_id) is None
                or type(token) is not str or not 32<=len(token)<=4096
                or type(provider_tenant) is not str or re.fullmatch('[A-Za-z0-9._:/-]{1,256}',provider_tenant) is None
                or type(binding) is not Delegation or not callable(current)):
            raise ValueError('fixed local service and verified delegation required')
        self.endpoint,self.service_id,self._token,self.binding,self.current=endpoint,service_id,token,binding,current
        self.provider_tenant=provider_tenant
        self._owned=client is None
        self.client=client or httpx.AsyncClient(trust_env=False,follow_redirects=False,
            limits=httpx.Limits(max_connections=8,max_keepalive_connections=4),timeout=2)

    async def search_evidence(self,*,query,actor_sub,workspace_id,max_evidence,context:KnowledgeReadContext):
        if (actor_sub,workspace_id)!=(self.binding.actor_sub,self.binding.workspace_id) or not self.current():
            raise RegionalKnowledgeAccessError('knowledge_delegation_unavailable')
        if time.monotonic()>=context.deadline_monotonic:return failure(context,'deadline_exceeded')
        body=dict(codec='rkb.knowledge-query.v2',query=query,actor_sub=actor_sub,workspace_id=workspace_id,
            provider_tenant=self.provider_tenant,
            link_id=self.binding.link_id,link_revision=self.binding.link_revision,request_id=context.request_id,
            turn_generation=context.turn_generation,min_data_seq=context.min_data_seq,
            deadline_monotonic=context.deadline_monotonic,max_evidence=max_evidence)
        started=time.monotonic();status='unavailable'
        try:
            async with __import__('asyncio').timeout_at(context.deadline_monotonic):
                async with self.client.stream('POST',self.endpoint,json=body,headers={
                        'Authorization':'Bearer '+self._token,'X-RKB-Service':self.service_id,
                        'Accept':'application/vnd.rkb.knowledge-search.v2+json'}) as response:
                    if response.status_code in (401,403):
                        raise RegionalKnowledgeAccessError('knowledge_access_denied')
                    if response.status_code!=200:return failure(context,'service_unavailable')
                    raw=bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw)>256*1024:raise RegionalKnowledgeContractError('knowledge_response_too_large')
                    value=json.loads(raw)
            if not self.current():raise RegionalKnowledgeAccessError('knowledge_delegation_changed')
            status=value.get('status','invalid') if isinstance(value,dict) else 'invalid'
            return value
        except (TimeoutError,httpx.TimeoutException):return failure(context,'deadline_exceeded')
        except httpx.RequestError:return failure(context,'service_unavailable')
        except (ValueError,TypeError):raise RegionalKnowledgeContractError('knowledge_response_invalid') from None
        finally:
            _LOG.info('regional_knowledge_read',extra={'event':'regional_knowledge_read','request_id':context.request_id,
                'status':status,'duration_ms':round((time.monotonic()-started)*1000,3)})

    async def aclose(self):
        if self._owned:await self.client.aclose()


class RegionalKnowledgeFactory:
    def __init__(self,store,path):
        self.store=store;self.config=PrivateFile(path,65536)
        value=json.loads(self.config.data)
        fields={'codec','endpoint','service_id','service_token_file','provider_tenant','bindings'}
        if (type(value) is not dict or set(value)!=fields or value['codec']!='ph.rkb-provider.v2'
                or type(value['provider_tenant']) is not str or not 1<=len(value['provider_tenant'])<=256
                or type(value['bindings']) is not list or not 1<=len(value['bindings'])<=32):
            raise ValueError('exact regional provider config required')
        self.secret=PrivateFile(value['service_token_file'],4096)
        token=self.secret.data.decode().strip()
        bindings=[Delegation(**row) for row in value['bindings']]
        if len({(row.actor_sub,row.workspace_id) for row in bindings})!=len(bindings):
            raise ValueError('unique regional account scopes required')
        self.adapters={}
        for row in bindings:
            def current(binding=row):
                if not self.config.current() or not self.secret.current():return False
                try:
                    with self.store._lock:self.store._membership(binding.actor_sub,binding.workspace_id)
                    return True
                except Exception:return False
            provider=RegionalKnowledgeHttpProvider(endpoint=value['endpoint'],service_id=value['service_id'],
                provider_tenant=value['provider_tenant'],token=token,binding=row,current=current)
            self.adapters[row.actor_sub,row.workspace_id]=RegionalKnowledgeAdapter(provider,row.actor_sub,row.workspace_id)
        self.provider_tenant=value['provider_tenant']

    def __call__(self,actor_sub,workspace_id):
        adapter=self.adapters.get((actor_sub,workspace_id))
        return adapter if adapter is not None and adapter.provider.current() else None

    async def aclose(self):
        for adapter in self.adapters.values():await adapter.provider.aclose()


def configured_knowledge_factory(store,environment=None):
    env=os.environ if environment is None else environment
    path=str(env.get('PROJECTS_HUB_RKB_CONFIG') or '').strip()
    if not path:return None
    try:return RegionalKnowledgeFactory(store,path)
    except (OSError,ValueError,TypeError,KeyError):
        _LOG.warning('regional knowledge configuration unavailable',extra={'event':'regional_knowledge_config','status':'unavailable'})
        return None
