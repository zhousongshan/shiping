"""Resume a bounded five-input acceptance run through the employee HTTP API.

No job retries or receipt clearing. Each submit needs a named case; idempotent
replays retain exactly the original payload. Fixtures and full evidence stay local.
"""
import argparse
import hashlib
import json
import time
from pathlib import Path
import requests
from app import config,store
from scripts.accept_materials import scrub

ROOT=config.DATA/'acceptance'/'2026-09-28-pig'
BASE='http://127.0.0.1:4780'
IMAGE=Path('/Users/miniso/Downloads/0829-ZSS-猪猪玩偶公仔2-超韧AI-盲盒 (1).png')
VIDEO=Path('/Users/miniso/Downloads/0825-ZSS-视频2-超韧AI-视频.mp4')
CASES={
 'image_reference':{'prompt':'','images':True,'reference':True,'duration':None},
 'text':{'prompt':'制作12秒横屏粉色系玩偶展示短片：主角是一只浅粉色毛绒小猪，闭眼细线刺绣、深粉色鼻子和蹄子，不穿衣服不戴帽子。在粉色背景的圆台上入场、可爱地摆动、展示正面和侧面，最后挥手告别。配轻快音乐，不要台词或字幕。','images':False,'reference':False,'duration':12},
 'text_image':{'prompt':'制作12秒横屏商品展示视频。以图片中这只闭眼浅粉色毛绒小猪为主体，保持耳朵、鼻子、蹄子和身体外观，不增加帽子或衣服。在粉色背景的圆台上可爱地入场、摆动，展示正面和侧面后挥手告别。配轻快音乐，不要台词或字幕。','images':True,'reference':False,'duration':12},
 'text_reference':{'prompt':'制作12秒横屏玩偶展示短片，借鉴参考视频的粉色场景、近景特写和商品展示节奏，主角为参考里的同一款绿色服装蓝色帽子小猪。可改编镜头顺序，完整开场和结尾。配轻快音乐，不要台词或字幕。','images':False,'reference':True,'duration':12},
 'all_inputs':{'prompt':'制作12秒横屏商品展示视频，使用图片中闭眼浅粉色毛绒小猪作为唯一商品主体，保持图片的脸、耳朵、身体和深粉蹄子，不变成参考里绿色服装蓝色帽子的款式。借鉴参考视频的粉色场景、特写和正侧面展示节奏，可以改编镜头和动作，不要求复刻挂件结构。配轻快音乐，不要台词或字幕。','images':True,'reference':True,'duration':12},
}

def write(path,value):
 temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2));temporary.replace(path)

def load():
 ROOT.mkdir(parents=True,exist_ok=True)
 path=ROOT/'execution.json'
 return json.loads(path.read_text()) if path.exists() else {'assets':{},'runs':{}}

def request(path,**kwargs):
 with requests.Session() as session:
  session.trust_env=False
  response=session.request('POST' if ('json' in kwargs or 'data' in kwargs) else 'GET',BASE+path,timeout=(10,180),**kwargs)
  response.raise_for_status();return response.json()

def submit(case):
 book=load()
 if case in book['runs'] and book['runs'][case].get('job_id'):
  print(json.dumps(book['runs'][case],ensure_ascii=False));return
 if not request('/api/ready')['ready']:raise ValueError('Worker not ready')
 health=request('/api/health')
 if not health['generation']['available']:raise ValueError(health['generation']['reason'])
 for name,path,kind in [('image',IMAGE,'image'),('video',VIDEO,'video')]:
  sha=hashlib.sha256(path.read_bytes()).hexdigest()
  if name in book['assets']:
   if book['assets'][name]['sha256']!=sha:raise ValueError('Acceptance fixture changed')
  else:
   asset=request('/api/assets',params={'kind':kind,'name':path.name},data=path.read_bytes(),headers={'Content-Type':'application/octet-stream'})
   book['assets'][name]={'id':asset['id'],'sha256':sha,'source':str(path)};write(ROOT/'execution.json',book)
 spec=CASES[case]
 payload={'prompt':spec['prompt'],'images':[book['assets']['image']['id']] if spec['images'] else [],
  'reference':book['assets']['video']['id'] if spec['reference'] else None,'duration':spec['duration'],
  'ratio':None if case=='image_reference' else '16:9','max_unit_generations':2,'timing_mode':'strict'}
 run=book['runs'].setdefault(case,{'request':payload,'key':'accept-20260928-pig-'+case.replace('_','-'),
  'created':time.time(),'source_version':config.SOURCE_VERSION})
 if run['request']!=payload:raise ValueError('Recorded request changed; do not submit a new paid attempt')
 write(ROOT/'execution.json',book)
 job=request('/api/jobs',json=payload,headers={'Idempotency-Key':run['key']})
 run['job_id']=job['id'];write(ROOT/'execution.json',book)
 print(json.dumps({'case':case,'job_id':job['id'],'status':job['status']},ensure_ascii=False))

def snapshot():
 book=load();rows=[]
 for case,run in book['runs'].items():
  if not run.get('job_id'):continue
  job=store.get(run['job_id'])
  folder=ROOT/'runs'/case;folder.mkdir(parents=True,exist_ok=True)
  write(folder/'latest.json',scrub({k:v for k,v in job.items() if k!='execution'}))
  rows.append({'case':case,'id':job['id'],'status':job['status'],'error':job.get('error'),
   'question':job.get('question') if job['status']=='needs_input' else None,
   'duration':job.get('duration'),'builds':len(job.get('agent_builds',{})),
   'generation_seconds':job.get('reserved_generation_seconds',0),
   'events':[e['message'] for e in job.get('events',[])[-2:]]})
 write(ROOT/'results.json',scrub(rows));print(json.dumps(scrub(rows),ensure_ascii=False))

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('action',choices=['submit','snapshot']);parser.add_argument('--case',choices=list(CASES))
 args=parser.parse_args()
 if args.action=='submit':
  if not args.case:parser.error('submit requires --case')
  submit(args.case)
 else:snapshot()
