"""Bounded live acceptance via the normal HTTP API; snapshots never generate."""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import urllib.parse
import urllib.request
from app import config, store

ROOT=config.DATA/'acceptance'/'2026-09-27-materials'
BASE='http://127.0.0.1:4780'
TERMINAL={'completed','failed','needs_review','needs_revision','needs_attention','needs_configuration','needs_input'}

def write(path,value):
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)

@contextlib.contextmanager
def locked():
    ROOT.mkdir(parents=True,exist_ok=True)
    with (ROOT/'execution.lock').open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        yield

def request(path,data=None,headers=None,timeout=180):
    with urllib.request.urlopen(urllib.request.Request(BASE+path,data=data,headers=headers or {}),timeout=timeout) as response:
        return json.load(response)

def load():
    p=ROOT/'execution.json'
    return json.loads(p.read_text()) if p.exists() else {'assets':{},'runs':[]}

def scrub(value,key=''):
    if any(s in key.lower() for s in ('secret','authorization','cookie','signature_value','api_key','token')):
        return '[redacted]'
    if isinstance(value,dict):return {k:scrub(v,k) for k,v in value.items()}
    if isinstance(value,list):return [scrub(v) for v in value]
    if isinstance(value,str):
        if value.startswith('data:'):return '[inline media omitted]'
        value=re.sub(r'Bearer\s+\S+','Bearer [redacted]',value,flags=re.I)
        return re.sub(r'https?://[^\s"<>]+',lambda m:m.group().split('?',1)[0],value)
    return value

def submit(case_id,attempt,reason):
    spec=json.loads((ROOT/'test-cases.json').read_text())
    case=next((c for c in spec['cases'] if c['case_id']==case_id),None)
    if not case or attempt not in (1,2):raise ValueError('Unknown case or attempt outside 1..2')
    with locked():
        book=load()
        same=next((r for r in book['runs'] if r['case_id']==case_id and r['attempt']==attempt),None)
        if same and same.get('job_id'):
            print(json.dumps({'reused':True,**same},ensure_ascii=False));return
        request_key=f'materials-20260927-{case_id}-{attempt}'
        approval=store.generation_authorization('local',request_key)
        approved=bool(approval and not approval['job_id'] and approval['expires']>time.time())
        if attempt==2:
            prior=next((r for r in book['runs'] if r['case_id']==case_id and r['attempt']==1),None)
            if not prior or not prior.get('job_id') or len(reason.strip())<5:raise ValueError('Second attempt requires the first run and a concrete reason')
            previous=store.get(prior['job_id'])
            if previous['status'] not in TERMINAL or ((previous.get('agent_submission_intent') or previous.get('submission_uncertain')) and not approved):
                raise ValueError('Prior run is still active or its receipt is uncertain; never resubmit')
        health=request('/api/health')
        if not same and not approved and health.get('generation',{}).get('available') is False:
            raise ValueError(health['generation']['reason'])
        if not request('/api/ready').get('ready'):raise ValueError('Worker not ready')
        if case['input']['reference'] and not health.get('reference_ready'):raise ValueError('Reference channel not configured')
        for alias in [*case['input']['images'],*([case['input']['reference']] if case['input']['reference'] else [])]:
            asset=spec['assets'][alias];source=Path(asset['path'])
            if hashlib.sha256(source.read_bytes()).hexdigest()!=asset['sha256']:raise ValueError('Fixture changed: '+alias)
            if alias not in book['assets']:
                kind='image' if alias=='I' else 'video'
                query=urllib.parse.urlencode({'kind':kind,'name':source.name})
                uploaded=request('/api/assets?'+query,source.read_bytes(),{'Content-Type':'application/octet-stream'})
                book['assets'][alias]={'id':uploaded['id'],'sha256':asset['sha256'],'uploaded_at':time.time()}
                write(ROOT/'execution.json',book)
        if same:
            payload=same['request'];request_key=same['idempotency_key']
        else:
            payload={**case['input'],'images':[book['assets'][a]['id'] for a in case['input']['images']],
                     'reference':book['assets'][case['input']['reference']]['id'] if case['input']['reference'] else None,
                     'max_unit_generations':1 if approved else 2}
            request_key=f'materials-20260927-{case_id}-{attempt}'
            same={'case_id':case_id,'attempt':attempt,'reason':reason,'request':payload,
                  'idempotency_key':request_key,'source_at_submission':config.SOURCE_VERSION,'requested_at':time.time()}
            if approved:same['explicit_authorization']={k:approval[k] for k in ('max_seconds','max_submissions','expires','note')}
            book['runs'].append(same)
            write(ROOT/'execution.json',book)  # Intent first; an HTTP retry uses exactly this key and payload.
        job=request('/api/jobs',json.dumps(payload,ensure_ascii=False).encode(),{'Content-Type':'application/json','Idempotency-Key':request_key})
        same.update(job_id=job['id'],acknowledged_at=time.time())
        write(ROOT/'execution.json',book)
        print(json.dumps({'case_id':case_id,'attempt':attempt,'job_id':job['id'],'status':job['status'],'max_unit_generations':job.get('max_unit_generations')},ensure_ascii=False))

def snapshot():
    with locked():
        book=load();summary=[]
        for run in book['runs']:
            if not run.get('job_id'):continue
            job=store.get(run['job_id']);folder=ROOT/'runs'/f"{run['case_id']}-{run['attempt']}"
            folder.mkdir(parents=True,exist_ok=True)
            clean=scrub({k:v for k,v in job.items() if k!='execution'})
            stable={k:v for k,v in clean.items() if k not in ('updated','queue_owner_last_claimed')}
            sha=hashlib.sha256(json.dumps(stable,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            latest=folder/'latest.json';stamp=folder/'last.sha256'
            if not stamp.exists() or stamp.read_text()!=sha:
                at=datetime.datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%f')
                write(folder/(at+'.json'),clean);write(latest,clean);stamp.write_text(sha)
            trace=config.DATA/'jobs'/job['id']/'step-trace.jsonl'
            if trace.is_file():shutil.copy2(trace,folder/'step-trace.jsonl')
            outputs=[]
            for bid,b in job.get('agent_builds',{}).items():
                if b.get('collected_path'):
                    p=config.DATA/'jobs'/job['id']/'project'/b['collected_path']
                    outputs.append({'build_id':bid,'unit_id':b.get('unit_id'),'path':str(p),'exists':p.is_file(),
                                    'sha256':b.get('collected_sha256'),'state':b.get('state')})
            summary.append({'case_id':run['case_id'],'attempt':run['attempt'],'job_id':job['id'],
                'status':job['status'],'error':scrub(job.get('error')),'duration':job.get('duration'),
                'reserved_generation_seconds':job.get('reserved_generation_seconds',0),'outputs':outputs,
                'final_video':job.get('video') if job['status']=='completed' else None,
                'final_review':scrub(job.get('final_review')),'stage_seconds':job.get('status_seconds'),
                'actual_cost':None,'cost_note':'待供应商实际账单核对；生成秒数不是人民币费用'})
        write(ROOT/'results.json',summary)
        print(json.dumps([{'case_id':r['case_id'],'attempt':r['attempt'],'job_id':r['job_id'],'status':r['status'],'error':r['error'],'outputs':len(r['outputs'])} for r in summary],ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['submit','snapshot'])
    parser.add_argument('--case');parser.add_argument('--attempt',type=int,default=1)
    parser.add_argument('--reason',default='首轮按已确认计划测试')
    args=parser.parse_args()
    if args.action=='submit':submit(args.case,args.attempt,args.reason)
    snapshot()
