"""Explicit live acceptance run. Uses real model APIs; resume reuses existing job IDs.
Run with isolated VIDEO_AGENT_DATA and VIDEO_AGENT_MEDIA_DATA pointing at the live signed media gateway storage.
"""
import concurrent.futures
import json
import os
import threading
from pathlib import Path
from app import config,store,media,intent,agent_runner

MANIFEST=config.DATA/'acceptance.json'
MAIN=Path(os.environ['VIDEO_AGENT_MEDIA_DATA'])

def setup():
    config.DATA.mkdir(parents=True,exist_ok=True)
    if MANIFEST.exists():return json.loads(MANIFEST.read_text())
    import sqlite3
    with sqlite3.connect(MAIN/'tasks.sqlite') as c:
        records=[json.loads(c.execute('select doc from assets where id=?',(aid,)).fetchone()[0]) for aid in ['4bc092fe401b465f84cd408f8453e7b1','e931de969d2a4c4da8f37bd3138c0fb9']]
    for r in records:store.put_asset(r)
    fixture=config.DATA/'fixtures';fixture.mkdir(exist_ok=True)
    short=fixture/'reference-short.mp4'
    media.command([config.FFMPEG,'-v','error','-y','-i',str(media.original_path(records[1])),'-t','4','-c:v','libx264','-c:a','aac',str(short)],timeout=180)
    ref=media.save_upload(short,'video','验收参考片段4秒.mp4')
    # Extend only the closing display of this known reference to exercise >15s input.
    long=fixture/'reference-long.mp4'
    media.command([config.FFMPEG,'-v','error','-y','-i',str(media.original_path(records[1])),'-vf','tpad=stop_mode=clone:stop_duration=5','-af','apad','-t','16','-c:v','libx264','-c:a','aac',str(long)],timeout=180)
    longref=media.save_upload(long,'video','验收长参考16秒.mp4')
    image=records[0]['id']
    cases=[
      ('text',dict(prompt='生成4秒竖屏写实短片：一支蓝色铅笔静放在浅色木桌上，暖阳照入，镜头缓缓推进。不出现人物、文字或台词。',images=[],reference=None,duration=None)),
      ('image',dict(prompt='',images=[image],reference=None,duration=None)),
      ('text_image',dict(prompt='展示图片中的同一个小黄鸡挂件，放在浅蓝色背景的圆台上，镜头缓慢拉近。保持实物结构，不新增人物，不添加字幕。',images=[image],reference=None,duration=4)),
      ('reference',dict(prompt='',images=[],reference=ref['id'],duration=None)),
      ('text_reference',dict(prompt='复刻参考视频里的小猪和入场、推镜动作，将背景改为浅蓝色，不增加台词或字幕。',images=[],reference=ref['id'],duration=None)),
      ('image_reference',dict(prompt='',images=[image],reference=ref['id'],duration=None)),
      ('all_inputs',dict(prompt='用图片的小黄鸡挂件替换参考中的小猪，保留入场和镜头节奏。背景改为浅蓝色，不增加字幕或台词。',images=[image],reference=ref['id'],duration=None)),
      ('long_reference',dict(prompt='',images=[image],reference=longref['id'],duration=None)),
    ]
    result=[]
    for name,data in cases:
        job=store.create({**intent.resolve({**data,'ratio':None}),'engine':'hypit-agent-v5','owner':'local','acceptance_case':name})
        result.append({'case':name,'job_id':job['id']})
    MANIFEST.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    return result

def run(case):
    j=store.get(case['job_id'])
    if j['status']=='completed':return
    # Resuming known IDs is explicit; never clear a submission marker or generation budget.
    if j.get('agent_submission_intent'):return
    print('START',case['case'],j['id'],flush=True)
    agent_runner.process(j,threading.Event())
    j=store.get(j['id'])
    print('END',case['case'],j['status'],j.get('error'),flush=True)

if __name__=='__main__':
    cases=setup()
    names=set(os.environ.get('ACCEPT_CASES','').split(','))-{''}
    if names:cases=[c for c in cases if c['case'] in names]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(run,cases))
    rows=[]
    for case in json.loads(MANIFEST.read_text()):
        j=store.get(case['job_id']);rows.append({**case,**{k:j.get(k) for k in ['status','duration','tool_calls','reserved_generation_seconds','error','final_review']}})
    (config.DATA/'results.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
