"""Live understanding/planning only, in isolated storage; never submits video."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',required=True)
    args=parser.parse_args()
    project=Path(__file__).resolve().parent.parent
    acceptance=project/'data/acceptance/2026-09-27-materials'
    spec=json.loads((acceptance/'test-cases.json').read_text())
    case=next(c for c in spec['cases'] if c['case_id']==args.case)
    folder=acceptance/'planning-only'/args.case
    folder.mkdir(parents=True,exist_ok=False)
    original_settings=json.loads((project/'data/runtime-settings.json').read_text())
    (folder/'runtime-settings.json').write_text(json.dumps({'proxy_bypass_hosts':original_settings.get('proxy_bypass_hosts',[])}))
    os.environ['VIDEO_AGENT_DATA']=str(folder)
    os.environ['VIDEO_AGENT_DATABASE_URL']=''
    os.environ['VIDEO_AGENT_VIDEO_KEY_FILE']=str(project/'data/autodl-video-key')
    from app import config,media,store,intent,agent_tools,production,pipeline,hypit_adapter
    # Defense in depth: this script has no permitted generation action.
    def no_build(*args,**kwargs):raise AssertionError('Video generation is forbidden in planning-only checks')
    hypit_adapter.build=no_build
    from scripts.accept_materials import scrub
    result={'case_id':args.case,'started_at':datetime.datetime.now().astimezone().isoformat(),
            'source_version':config.SOURCE_VERSION,'scope':'real model understanding and planning only',
            'video_submission_attempts':0,'stages':[],'status':'running'}
    def save():
        (folder/'result.json').write_text(json.dumps(scrub(result),ensure_ascii=False,indent=2)+'\n')
    def stage(name,fn):
        item={'name':name,'started_at':datetime.datetime.now().astimezone().isoformat()}
        result['stages'].append(item);save();start=time.monotonic()
        print(json.dumps({'case':args.case,'stage':name,'state':'started'}),flush=True)
        try:
            value=fn();item['success']=True;return value
        except Exception as exc:
            item.update(success=False,error=str(exc),error_type=type(exc).__name__);raise
        finally:
            item['elapsed_seconds']=round(time.monotonic()-start,3);save()
    def dispatch(action,data=None):
        if action not in ('understand_subjects','analyze_reference','set_plan','ask'):
            raise AssertionError('Action not allowed: '+action)
        return agent_tools.dispatch(job['id'],action,data or {})
    started=time.monotonic();job=None
    try:
        uploaded={}
        for alias in [*case['input']['images'],*([case['input']['reference']] if case['input']['reference'] else [])]:
            asset=spec['assets'][alias];path=Path(asset['path'])
            if hashlib.sha256(path.read_bytes()).hexdigest()!=asset['sha256']:raise ValueError('Fixture changed')
            uploaded[alias]=stage('upload_'+alias,lambda:media.save_upload(path,'image' if alias=='I' else 'video',path.name))['id']
        payload={**case['input'],'images':[uploaded[a] for a in case['input']['images']],
                 'reference':uploaded.get(case['input']['reference']),'max_unit_generations':2}
        job=store.create({**intent.resolve(payload),'owner':'local','production_version':2,'workflow_version':3,
                          'source_version':config.SOURCE_VERSION,'engine':'hypit-agent-v5'})
        result['isolated_job_id']=job['id']
        root=stage('prepare',lambda:agent_tools.prepare(job));production.initialize(job,root)
        stage('understand_subjects',lambda:dispatch('understand_subjects'))
        if store.get(job['id'])['status']!='needs_input' and job.get('reference'):
            for _ in range(24):
                analysis=stage('analyze_reference',lambda:dispatch('analyze_reference'))
                if analysis.get('status')!='in_progress':break
            if analysis.get('status')!='verified':raise ValueError('Reference not verified')
        if store.get(job['id'])['status']!='needs_input':
            stage('plan_content',lambda:pipeline.make_plan(store.get(job['id']),root,dispatch))
        checked=store.get(job['id'])
        result.update(status='needs_input' if checked['status']=='needs_input' else 'plan_verified',
                      question=checked.get('question'),plan=checked.get('plan'),subject_spec=checked.get('subject_spec'),
                      reference_summary={k:checked.get('reference_analysis',{}).get(k) for k in ('status','duration','events','temporal_coverage')})
    except Exception as exc:
        result.update(status='failed',error=str(exc),error_type=type(exc).__name__)
    finally:
        result['elapsed_seconds']=round(time.monotonic()-started,3)
        if job:
            checked=store.get(job['id'])
            (folder/'job.json').write_text(json.dumps(scrub(checked),ensure_ascii=False,indent=2)+'\n')
            if checked.get('agent_builds') or checked.get('agent_submission_intent'):
                raise AssertionError('Planning-only test unexpectedly reached submission')
        save();print(json.dumps({k:result.get(k) for k in ('case_id','status','error','elapsed_seconds','video_submission_attempts')},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
