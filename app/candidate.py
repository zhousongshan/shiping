"""User-requested local assembly; never silently claims visual approval."""
import json
import os
import fcntl
from functools import lru_cache
from shutil import copy2
from . import config, store, media
from .production_schema import file_hash, fingerprint

ACTIVE = {'queued','planning','agent_running','waiting_build','generating','rendering'}

@lru_cache(maxsize=256)
def cached_hash(path,mtime,size):
    return file_hash(path)

@lru_cache(maxsize=256)
def clip_info(path,mtime,size):
    try:
        info=media.probe(path)
        return {'duration':float(info['format']['duration']),
                'has_audio':any(s.get('codec_type')=='audio' for s in info['streams'])}
    except Exception:return {'duration':None,'has_audio':None}

def available(job):
    root = config.DATA/'jobs'/job['id']/'project'
    results = []
    for b in job.get('agent_builds', {}).values():
        if b.get('state') != 'complete' or not b.get('generation_seconds'):
            continue
        name = b.get('collected_path')
        # Historical task convention, only if tied to a recorded successful generated hash.
        if not name:
            from pathlib import Path
            name = Path(b['run']).stem + '_clip.mp4'
        p = (root/name).resolve()
        if p.is_relative_to(root.resolve()) and p.is_file() and cached_hash(str(p),p.stat().st_mtime_ns,p.stat().st_size) in job.get('generated_hashes', []):
            version=job.get('generation_manifests',{}).get(b.get('manifest_id'),{}).get('revision',1)
            sha=cached_hash(str(p),p.stat().st_mtime_ns,p.stat().st_size)
            review=job.get('unit_reviews',{}).get(b.get('unit_id'),{})
            verdict=review.get('verdict') if review.get('original_sha256',review.get('sha256'))==sha else None
            from .review_policy import REVIEW_VERSION
            historical=bool(verdict and review.get('review_version')!=REVIEW_VERSION)
            results.append({'build_id': b['build_id'], 'unit_id': b.get('unit_id'), 'path':name,
                'version':version,'file_version':sha,'quality_status':'historical' if historical else verdict or 'unchecked',
                'review_summary':('历史自动检查记录（尚未按新版复核）：' if historical else '')+str(review.get('summary') or '') if verdict else None,
                'preview_url':f'/api/jobs/{job["id"]}/versions/{b["build_id"]}',
                **clip_info(str(p),p.stat().st_mtime_ns,p.stat().st_size),
                'title':f'片段 {b.get("unit_id") or len(results)+1} · 版本 {version}'})
    return results

def compose(job, build_ids):
    from .execution import maintain
    claimed=store.claim_local(job['id'],('completed','failed','needs_attention','needs_review','needs_revision','needs_input','needs_configuration'))
    with maintain(claimed):return _compose(claimed,build_ids)

def _compose(job, build_ids):
    if job['status'] in ACTIVE:
        raise ValueError('请等待正在运行的制作回合结束，再合成已有片段')
    available_by_id = {x['build_id']:x for x in available(job)}
    if not build_ids or len(build_ids) != len(set(build_ids)) or any(i not in available_by_id for i in build_ids):
        raise ValueError('请选择本任务已保存的生成片段，不能使用其他文件')
    units=[available_by_id[i]['unit_id'] for i in build_ids if available_by_id[i]['unit_id']]
    if len(units)!=len(set(units)):
        raise ValueError('同一生成单元只能选择一个版本；多个版本是替代关系，不能一起拼接')
    order={u['id']:n for n,u in enumerate((job.get('plan') or {}).get('units',[]))}
    build_ids=sorted(build_ids,key=lambda i:order.get(available_by_id[i]['unit_id'],len(order)))
    root = config.DATA/'jobs'/job['id']; project = root/'project'
    sources = [project/available_by_id[i]['path'] for i in build_ids]
    version = fingerprint([file_hash(p) for p in sources])
    dest = root/'candidates'/f'{version}.mp4';dest.parent.mkdir(exist_ok=True)
    with (root/'candidate.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if not dest.is_file():
            width,height={'9:16':(720,1280),'1:1':(720,720)}.get(job.get('ratio'),(1280,720))
            cmd = [config.FFMPEG,'-v','error','-y'];filters=[];ports=[]
            for p in sources:cmd += ['-i',str(p)]
            for i,p in enumerate(sources):
                info=media.probe(p);duration=float(info['format']['duration'])
                filters.append(f'[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,setpts=PTS-STARTPTS[v{i}]')
                if any(s['codec_type']=='audio' for s in info['streams']):
                    filters.append(f'[{i}:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]')
                else:filters.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={duration},asetpts=PTS-STARTPTS[a{i}]')
                ports += [f'[v{i}]',f'[a{i}]']
            filters.append(''.join(ports)+f'concat=n={len(sources)}:v=1:a=1[v][a]')
            temp=dest.with_suffix('.pending.mp4')
            media.command(cmd+['-filter_complex',';'.join(filters),'-map','[v]','-map','[a]',
                '-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p','-c:a','aac','-movflags','+faststart',str(temp)],timeout=300)
            media.command([config.FFMPEG,'-v','error','-i',str(temp),'-f','null','-'],timeout=180)
            temp.replace(dest)
        copy2(dest,root/'candidate.pending.mp4');os.replace(root/'candidate.pending.mp4',root/'candidate.mp4')
    info=media.probe(dest)
    record={'version':version,'path':str(dest),'build_ids':build_ids,'duration':float(info['format']['duration']),
        'technical_status':'playable','quality_status':'not_approved','source':'user_requested_existing_clips'}
    store.update(job['id'], candidate_delivery=record, **({'status':'needs_review'} if job['status']=='completed' else {}))
    store.event(job['id'],'已合成所选已有片段，可预览下载；此操作不代表内容检查通过')
    return record
