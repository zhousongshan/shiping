"""Task-bound tools used by Harness. The model never receives credentials or shell access."""
import base64
import hashlib
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from shutil import copy2
from . import config, store, media, planner, compiler, hypit_adapter as hypit
from . import production, production_schema as ps, workflows, creative_brief, structured

USAGE = {
 'generate_unit':'{unit_id,prompt,images?:[relative image paths],duration?:integer4..15,first_frame?,last_frame?}; preferred: automatically cut the planned reference, attach accepted dependency frames, create source, validate and submit once. No name/run/output/videos needed. pending means end this turn.',
 'collect_review':'{build_id}; preferred for a completed generation: collect output and review its unit, return adopted path and end frame. Never poll or regenerate a pending Build.',
 'compose_build':'{clips?:[{unit_id,path,duration}],audio?:relative audio path,mute?:boolean}; preferred: default to all accepted units in plan order; compose, validate and start Hypit rendering in one call. pending means end this turn.',
 'analyze_reference':'{}; detect candidates, inspect all intervals and verify actual reference events',
 'understand_subjects':'{}; inspect actual images and identify roles and whole-subject replacement rules',
 'resolve_subject_mapping':'{}; resolve which observed reference identity each subject image replaces, asking when ambiguous',
 'review_unit':'{unit_id,path}; inspect generated unit and persist accepted end frame or failure evidence',
 'docs':'{name: "seedance"|"film"|"script"|"media"|"example"|"reference"}',
 'list':'{}', 'read':'{path}', 'write':'{path,content}; only project .svml/.svrun/.svs/.md/.json',
 'observe':'{path,start?,end?,count?:1..16}; actual images returned, seconds relative to that file',
 'analyze_video':'{path,start?,end?,fps?:2..5,question?}; max 30s per request, video and audio analyzed separately',
 'cut':'{path,start,end,to}; lossless-intent re-encode to MP4, full audio retained',
 'frame':'{path,at:"first"|"last"|seconds,to:relative.jpg}; extract a real generated frame for next unit continuity',
 'extract_audio':'{path,to:relative.wav}; extract actual audio for shared soundtrack or inspection',
 'conform_duration':'{path,duration,to}; slight retime up to 5 percent for generation rounding; keeps audio in sync',
 'set_plan':'{theme,story,segments:[{title,description,duration}],units:[{id,description,duration,event_ids?,reference_range?,depends_on:[],continuity:{type,reason,boundary_reason?}}]}; duration>15s requires units covering total; each unit<=15s, continuity type independent|same_action|new_angle|new_scene; unit may contain several narrative segments',
 'generation_source':'{unit_id,name,prompt,duration:integer4..15,images?:[relative JPG paths],videos?:[relative MP4 paths],first_frame?,last_frame?}; creates correct editable Hypit source/run, returns exact build arguments',
 'generate_subject':'{prompt}; create one shared subject image for text-only multi-unit stories, then observe it. Reuses saved image.',
 'check':'{run}; check Source and Run',
 'build':'{run,output}; start once. On running return end your turn; supervisor resumes on completion.',
 'status':'{build_id}', 'collect':'{build_id,to}; collect the output declared at build',
 'compose':'{clips:[{path,duration}],audio?:relative audio path,mute?:boolean}; generate complete editable film.svml/svrun; trims by timeline; mute removes all sound',
 'review':'{path}; independently inspect real output against inputs, returns pass/fail/warn',
 'finish':'{path}; requires current passing review and correct duration, cannot self-approve',
 'ask':'{question}; only for an essential ambiguous choice; suspends until the employee answers',
}

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def review_context(job):
    if job.get('production_version') == 2:return ps.context_version(job)
    return hashlib.sha256(json.dumps({k:job.get(k) for k in ['prompt','effective_prompt','images','reference','duration','ratio','plan']},sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def record_generated(jid,path):
    hashes=store.get(jid).get('generated_hashes',[])
    value=digest(path)
    if value not in hashes:store.update(jid,generated_hashes=[*hashes,value])

def safe(root,name):
    if not isinstance(name,str) or not name:raise ValueError('Missing relative project path')
    path=(root/name).resolve()
    if not path.is_relative_to(root.resolve()) or any(x.startswith('.') for x in Path(name).parts if x!='.'):
        raise ValueError('Path is outside the visible project')
    return path

def output_path(root,name,job):
    path=safe(root,name)
    immutable=[root/'assets'/'reference.mp4',*(root/'assets'/f'{aid}.jpg' for aid in job['images'])]
    if path in immutable or path.name.startswith('hypit') or path.name.endswith('.cut.json') or any(part in path.relative_to(root).parts for part in ('submission-journal','production','evidence')):
        raise ValueError('Original inputs and execution receipts cannot be overwritten')
    return path

def prepare(job):
    root=config.DATA/'jobs'/job['id']/'project';root.mkdir(parents=True,exist_ok=True)
    assets=root/'assets';assets.mkdir(exist_ok=True)
    for aid in job['images']:
        a=store.asset(aid)
        if a.get('owner','local')!=job.get('owner','local'):raise ValueError('Asset ownership mismatch')
        target=assets/f'{aid}.jpg'
        if not target.exists():copy2(a['path'],target)
    if job.get('reference') and not (assets/'reference.mp4').exists():
        a=store.asset(job['reference'])
        if a.get('owner','local')!=job.get('owner','local'):raise ValueError('Asset ownership mismatch')
        # Normalize original bytes into a correctly labelled MP4, including audio.
        media.command([config.FFMPEG,'-v','error','-y','-i',str(media.original_path(a)),
          '-map','0:v:0','-map','0:a:0?','-vf','scale=1280:1280:force_original_aspect_ratio=decrease:force_divisible_by=2',
          '-c:v','libx264','-crf','19','-c:a','aac',str(assets/'reference.mp4')],timeout=300)
    hypit.setup(root)
    return root

def range_clip(root,path,args,to=None):
    info=media.probe(path);duration=float(info['format']['duration'])
    start=float(args.get('start',0));end=float(args.get('end',duration))
    if not all(math.isfinite(v) for v in [start,end]) or not 0<=start<end<=duration+.05:
        raise ValueError('Invalid video interval')
    target=to or root/'evidence'/f'{digest(path)[:12]}-{start:g}-{end:g}.mp4'
    if target==path:raise ValueError('Cannot overwrite source media')
    target.parent.mkdir(parents=True,exist_ok=True)
    receipt=target.with_suffix('.cut.json')
    provenance={'source_sha256':digest(path),'start':start,'end':end}
    reusable=target.exists() and receipt.exists() and all(json.loads(receipt.read_text()).get(k)==v for k,v in provenance.items()) and json.loads(receipt.read_text()).get('output_sha256')==digest(target)
    if not reusable:
        temporary=target.with_name(target.stem+'.pending.mp4')
        media.command([config.FFMPEG,'-v','error','-y','-ss',str(start),'-i',str(path),'-t',str(end-start),
          '-map','0:v:0','-map','0:a:0?','-c:v','libx264','-crf','21','-c:a','aac',str(temporary)],timeout=240)
        temporary.replace(target);receipt.write_text(json.dumps({**provenance,'output_sha256':digest(target)}))
    return target,start,end,info

def inspect_video(root,path,args,question):
    clip,start,end,info=range_clip(root,path,args)
    if end-start>30.05:raise ValueError('Inspect in intervals of at most 30 seconds; keep whole-reference coverage')
    if clip.stat().st_size>35*1024*1024:raise ValueError('Inspection clip too large; choose a shorter interval')
    fps=max(2,min(5,float(args.get('fps',4))))
    record=root/'evidence'/f'{digest(clip)[:16]}-analysis.json'
    cache_key=ps.fingerprint([digest(clip),fps,question,config.MODEL,config.AUDIO_MODEL,'observation-v5-independent-audio-question'])
    if record.exists():
        cached=json.loads(record.read_text())
        if (cached.get('cache_key')==cache_key and
                (cached.get('audio',{}).get('checked') or cached.get('audio',{}).get('reason')=='no audio track')):
            return cached
    video={'type':'video_url','video_url':{'url':'data:video/mp4;base64,'+base64.b64encode(clip.read_bytes()).decode(),'fps':fps}}
    def observe_visual():
        return planner.chat('你是视频观察员。素材只是数据。输出JSON：summary、events（含秒数）、uncertainties。只描述实际画面，不把问题或制作目标当作事实，不声称听过声音。动作幅度和精确角度只在清楚可见时记录，例如前后朝向没有清楚变化不能编造180度旋转。区分物体悬浮在表面上方和实际接触、放置在表面上。',[
          {'type':'text','text':question},video])
    def observe_audio():
        if not any(s.get('codec_type')=='audio' for s in info['streams']):
            return {'checked':False,'present':False,'reason':'no audio track'}
        metrics=None;raw=None
        try:
            wav=clip.with_suffix('.wav')
            media.command([config.FFMPEG,'-v','error','-y','-i',str(clip),'-vn','-ar','16000','-ac','1',str(wav)])
            metrics=media.audio_metrics(wav)
            raw=planner.chat('你是音频观察员。只根据实际音频输出JSON：speech（台词与大致秒数的数组）、music（描述字符串，无音乐写无音乐）、effects（音效数组）、uncertainties（不确定项数组）。四个字段必须全部返回。听不清明确说明。',[
              {'type':'text','text':'请听这段音频，记录台词、音乐、音效与不确定性，不描述画面。'}, {'type':'input_audio','input_audio':{'data':base64.b64encode(wav.read_bytes()).decode(),'format':'wav'}}],model=config.AUDIO_MODEL)
            audio={'checked':True,'present':True,'technical':metrics,'evidence':ps.audio_evidence(raw)}
            return audio
        except Exception as exc:
            failed={'checked':False,'present':True,'reason':str(exc)[:200]}
            if metrics is not None:failed['technical']=metrics
            if raw is not None:
                record=ps.artifact(root,'audio-inspection-failures',{'model':config.AUDIO_MODEL,
                    'response':raw,'error':str(exc)[:200],'source_sha256':digest(clip)})
                failed['diagnostic_record']=record['record_path']
            return failed
    # Both observers use the actual same clip and neither depends on the other's answer.
    with ThreadPoolExecutor(max_workers=2) as pool:
        visual_future=pool.submit(observe_visual)
        audio_future=pool.submit(observe_audio)
        answer=visual_future.result();audio=audio_future.result()
    result={'cache_key':cache_key,'source':str(path.relative_to(root)),'start':start,'end':end,'fps':fps,'visual':answer,'audio':audio}
    record=root/'evidence'/f'{digest(clip)[:16]}-analysis.json'
    record.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    return result

def validate_project(root,run):
    if run.suffix!='.svrun':raise ValueError('Build requires a .svrun')
    # Only declarative built-in Hypit components in this first deployment.
    for p in [*root.rglob('*.svml'),*root.rglob('*.svrun'),*root.rglob('*.svs')]:
        if '.hypit' in p.parts or 'node_modules' in p.parts:continue
        text=p.read_text()
        for key,val in re.findall(r'\b(from|source|src)\s*=\s*[\"\']([^\"\']+)[\"\']',text):
            if key=='from':
                allowed={'media','text','seedance','film','script','media-track','audio-track','timeline-author','spatial','media-pipeline','render-hyperframes','caption','typography-track'}
                if val not in {f'@hypit/{name}@1' for name in allowed}:raise ValueError('This Hypit module is not enabled for task authoring')
            else:
                target=(p.parent/val).resolve()
                if not target.is_relative_to(root.resolve()) or 'node_modules' in target.parts or '.hypit' in target.parts:
                    raise ValueError('Source references outside project inputs are forbidden')
    return hypit.check(root,run)

def generation_seconds(sources):
    total=0
    for p in sources:
        if p.suffix!='.svml':continue
        text=p.read_text()
        aliases=re.findall(r'<import\s+as="([\w-]+)"\s+from="@hypit/seedance@1"',text)
        if '@hypit/seedance@1' in text and not aliases:
            raise ValueError('Use the documented import as="seedance" from="@hypit/seedance@1" format')
        for alias in aliases:
            for attrs in re.findall(r'<'+re.escape(alias)+r':(?:TextVideo|FrameVideo|ReferenceVideo)\b([^>]*)>',text):
                found=re.search(r'\bduration="(\d+)"',attrs)
                if not found or not 4<=int(found[1])<=15:raise ValueError('Generation duration must be a literal integer from 4 to 15')
                total+=int(found[1])
    return total

def reference_coverage(notes,duration):
    end=0.0
    for n in sorted(notes,key=lambda n:n['start']):
        if n['start']>end+.1:return False
        end=max(end,n['end'])
    return end>=duration-.1

def review_joins(root,path,job):
    composition=job.get('composition') or {}
    boundaries=composition.get('boundaries',[])
    result=[]
    duration=float(media.probe(path)['format']['duration'])
    for at in boundaries:
        clip,start,end,info=range_clip(root,path,{'start':max(0,at-1.2),'end':min(duration,at+1.2)})
        content=[{'type':'text','text':json.dumps({'cut_at':at-start,'goal':job.get('effective_prompt',job['prompt']),'plan':job.get('plan')},ensure_ascii=False)},
          {'type':'video_url','video_url':{'url':'data:video/mp4;base64,'+base64.b64encode(clip.read_bytes()).decode(),'fps':4}}]
        for aid in job['images']:
            content.extend([{'type':'text','text':'固定主体/场景基准图'},planner.image_content(root/'assets'/f'{aid}.jpg')])
        # Native video sampling can skip a fast but valid movement at the cut.
        # Supply real neighboring frames with times instead of inferring a jump.
        dense=root/'evidence'/f'{digest(path)[:16]}-join-{at:g}';dense.mkdir(exist_ok=True)
        for offset in [-.16,-.04,0,.04,.12,.32,.6]:
            t=max(0,min(duration-.04,at+offset));frame=dense/f'{t:.5f}.jpg'
            if not frame.exists():media.command([config.FFMPEG,'-v','error','-y','-ss',str(t),'-i',str(path),'-frames:v','1','-vf','scale=640:-2','-pix_fmt','yuvj420p',str(frame)])
            if frame.exists():content.extend([{'type':'text','text':f'拼接点相对时间 {t-at:+.3f} 秒的实际帧，判断快速动作是否有连续中间状态'},planner.image_content(frame)])
        r=planner.chat('检查这段真实视频中间的拼接点。允许符合剧情的换景/换机位；检查主体身份是否改变、重复动作、跳动作、丢失关键状态、黑闪。只对实际画面判断，不声称听过声音。输出JSON：verdict(pass/fail/warn)、issues数组、summary。无法判定用warn。',content)
        if r.get('verdict') not in ['pass','fail','warn'] or not isinstance(r.get('issues'),list):raise ValueError('Invalid join review')
        if any(s.get('codec_type')=='audio' for s in info['streams']):
            try:
                wav=clip.with_suffix('.join.wav')
                media.command([config.FFMPEG,'-v','error','-y','-i',str(clip),'-vn','-ar','16000','-ac','1',str(wav)])
                a=planner.chat('只依据实际音频检查拼接点有无突兀换音乐、断字、重复台词、爆音或异常静音。允许自然收束、合理淡入淡出与按剧情切换，不因音乐有变化就判失败。输出JSON：verdict(pass/fail/warn)、issues数组、summary。不声称看过画面。',[
                    {'type':'text','text':f'拼接点在这段音频的{at-start:.2f}秒，任务：'+job.get('effective_prompt',job['prompt'])},
                    {'type':'input_audio','input_audio':{'data':base64.b64encode(wav.read_bytes()).decode(),'format':'wav'}}],model=config.AUDIO_MODEL)
                if a.get('verdict') not in ['pass','fail','warn'] or not isinstance(a.get('issues'),list):raise ValueError('Invalid audio join review')
            except Exception:
                a={'verdict':'warn','issues':['拼接点声音检查未完成'],'summary':'无法确认声音衔接'}
            r['audio']=a;r['issues'].extend(a['issues'])
            if a['verdict']=='fail':r['verdict']='fail'
            elif a['verdict']=='warn' and r['verdict']=='pass':r['verdict']='warn'
        else:r['audio']={'verdict':'pass','issues':[],'summary':'成片无音轨'}
        result.append({'at':at,**r})
    return result

def source_dependencies(root,run):
    seen=set()
    def visit(path):
        if path in seen:return
        seen.add(path)
        if path.suffix not in ['.svml','.svrun','.svs']:return
        for value in re.findall(r'\b(?:source|src)\s*=\s*[\"\']([^\"\']+)[\"\']',path.read_text()):
            target=(path.parent/value).resolve()
            if not target.is_relative_to(root.resolve()):raise ValueError('External dependency')
            if target.is_file():visit(target)
    visit(run)
    return sorted(seen)

def build_stamp(root,run,output,version=2):
    sources=source_dependencies(root,safe(root,run))
    profile=root/'hypit.runtime.json'
    provider=json.loads(profile.read_text()) if profile.is_file() else {}
    # Media host/signature rotation must not make an accepted generation look new.
    for endpoint in provider.get('endpoints',{}).values():
        endpoint.get('config',{}).pop('mediaBaseUrl',None)
    return hashlib.sha256((''.join(str(p.relative_to(root))+digest(p) for p in sorted(sources))+run+output+(json.dumps(provider,sort_keys=True) if version>=2 else '')).encode()).hexdigest()


def dispatch(jid,action,args):
    store.assert_execution(jid)
    started=time.monotonic();failed=True
    try:
        job=store.get(jid)
        count=job.get('tool_calls',0)+1
        if count>config.MAX_TOOL_CALLS:raise ValueError('工具调用次数达到上限，请查看任务记录后继续')
        store.update(jid,tool_calls=count)
        message={'context':'读取制作任务','understand_subjects':'识别主体与素材用途',
            'resolve_subject_mapping':'确认参考角色与替换范围',
            'analyze_reference':'分析并复核参考视频','observe':'查看画面','analyze_video':'分析视频与声音',
            'cut':'准备参考片段','set_plan':'确定制作方案','generation_source':'准备生成工程',
            'generate_unit':'准备并提交片段生成','collect_review':'下载并检查生成片段',
            'review_unit':'检查生成片段','compose':'编排完整视频','compose_build':'开始合成完整视频',
            'check':'校验制作工程','build':'提交制作任务','collect':'下载制作结果',
            'review':'检查完整视频','finish':'提交成片'}.get(action,'处理制作工程')
        store.event(jid,message)
        result=_dispatch(jid,action,args)
        failed=False
        return result
    finally:
        try:store.record_tool_timing(jid,action,time.monotonic()-started,failed)
        except Exception:pass  # Timing failures cannot turn a successful submission into a retry.


def _dispatch(jid,action,args):
    store.assert_execution(jid)
    from .telemetry import step
    with step(jid,action):
        return _execute(jid,action,args)

def _execute(jid,action,args):
    if not isinstance(args,dict):raise ValueError('args must encode an object')
    job=store.get(jid);root=prepare(job).resolve()
    if action in ('generate_unit','collect_review','compose_build'):
        return getattr(workflows,action)(job,root,args,_dispatch)
    if action=='analyze_reference':
        from .reference_analysis import analyze,summary
        result=analyze(job,root)
        return summary(result) if result.get('status')=='verified' else result
    if action=='understand_subjects':
        from .subject_spec import analyze
        return analyze(job,root)
    if action=='resolve_subject_mapping':
        from .subject_mapping import resolve
        return resolve(job,root)
    if action=='review_unit':
        from .unit_review import review
        return workflows.review_summary(review(job,root,args))
    if action=='context':
        return {'goal':job.get('effective_prompt',job['prompt']),'user_text':job['prompt'],'inferred':job.get('inferred_intent'),
          'duration':job.get('duration'),'ratio':job['ratio'],'reference_mode':job.get('reference_mode'),
          'images':[f'assets/{a}.jpg' for a in job['images']],
          'reference':'assets/reference.mp4' if job.get('reference') else None,
          'files':[str(p.relative_to(root)) for p in root.glob('*') if p.is_file() and not p.name.startswith('hypit.runtime')],
          'builds':job.get('agent_builds',{}),'usage':USAGE,'feedback':job.get('feedback'),
          'plan':job.get('plan'),'composition':job.get('composition'),
          'production_version':job.get('production_version'),'reference_analysis':__import__('app.reference_analysis',fromlist=['summary']).summary(job.get('reference_analysis')),
          'subject_spec':job.get('subject_spec'),'unit_reviews':{k:workflows.review_summary(v) for k,v in job.get('unit_reviews',{}).items()},'timing_policy':job.get('timing_policy'),
          'planning_hint':'目标与参考均在15秒内时，优先一个单元覆盖全部事件和镜头；观察区间不是生成分段。仅在内容确实需要时拆分并说明理由。',
          'observations':[str(p.relative_to(root)) for p in (root/'evidence').glob('*-analysis.json')],
          'capabilities':{**production.capabilities(root),
             'video_reference_ready':bool(config.media_base_url()),'composition':'Hypit declarative built-in modules',
             'audio_understanding':'probe through analyze_video; failures are explicit'}}
    if action=='docs':
        name=args['name']
        if name=='example':
            return {'generation':(config.ROOT/'agent-tools'/'generation-example.svml').read_text(),
                    'run':'<?svml using="@hypit/run-markup@1"?>\n<svrun version="1"><author source="./segment.svml"/><target output="clip.video"/></svrun>',
                    'composition':'Use compose for sequential clips; edit its resulting film.svml for precise custom timeline, subtitles and effects.'}
        if name=='reference':return {'text':(config.ROOT/'agent-tools'/'reference-guide.md').read_text()}
        if name not in ['seedance','film','script','media','media-track','audio-track','timeline-author','caption','typography-track','gpt-image']:
            raise ValueError('Unknown documentation module')
        p=config.ROOT/'node_modules/@hypit/hypit/packages'/name/'README.md'
        return {'text':p.read_text()[:24000]}
    if action=='list':return {'files':[str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and not any(x.startswith('.') or x=='node_modules' for x in p.relative_to(root).parts)][:250]}
    if action in ['read','write']:
        p=safe(root,args['path'])
        if p.suffix not in ['.svml','.svrun','.svs','.md','.json'] or p.name.startswith('hypit'):raise ValueError('Not an editable project document')
        if action=='read':return {'content':p.read_text()[:50000]}
        if p.name in ['plan.json','review.json'] or p.name.endswith('.cut.json') or p.relative_to(root).parts[0] in ['evidence','assets','submission-journal','production']:
            raise ValueError('Inputs, tool evidence and receipts are read-only; use dedicated tools')
        content=args['content']
        if len(content)>150000:raise ValueError('Document too large')
        p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content)
        return {'saved':args['path']}
    if action=='observe':
        p=safe(root,args['path']);count=max(1,min(16,int(args.get('count',8))))
        if p.suffix.lower() in ['.jpg','.jpeg','.png']:
            from PIL import Image
            to=root/'evidence'/f'{digest(p)[:16]}.jpg';to.parent.mkdir(exist_ok=True)
            with Image.open(p) as im:im.convert('RGB').save(to)
            return {'path':args['path'],'image_paths':[str(to)]}
        clip,start,end,_=range_clip(root,p,args)
        frames=media.frames(clip,root/'evidence'/f'{digest(clip)[:16]}-frames',count)
        return {'source':args['path'],'times':[round(start+t,3) for t,_ in frames], 'image_paths':[str(f) for _,f in frames]}
    if action=='analyze_video':return inspect_video(root,safe(root,args['path']),args,args.get('question','分析事件、镜头、动作、字幕、风格和节奏，为复刻提供具体时间信息。'))
    if action=='cut':
        p=safe(root,args['path']);to=output_path(root,args['to'],job)
        if to.suffix!='.mp4':raise ValueError('Cuts must be .mp4')
        result,start,end,_=range_clip(root,p,args,to)
        if digest(p) in job.get('generated_hashes',[]):record_generated(jid,result)
        return {'path':str(result.relative_to(root)),'start':start,'end':end}
    if action in ['frame','extract_audio','conform_duration']:
        p=safe(root,args['path']);to=output_path(root,args['to'],job);info=media.probe(p);duration=float(info['format']['duration'])
        if p==to:raise ValueError('Do not overwrite source media')
        to.parent.mkdir(parents=True,exist_ok=True)
        if action=='frame':
            if to.suffix not in ['.jpg','.png']:raise ValueError('Frame output must be JPG or PNG')
            at=args.get('at','last');temp=to.with_name(to.stem+'.pending'+to.suffix);temp.unlink(missing_ok=True)
            if at=='last':
                cmd=[config.FFMPEG,'-v','error','-y','-sseof',str(-min(1,duration)),'-i',str(p),'-vf','reverse']
            else:
                at=0 if at=='first' else float(at)
                if not 0<=at<duration:raise ValueError('Frame time outside video')
                cmd=[config.FFMPEG,'-v','error','-y','-ss',str(at),'-i',str(p)]
            media.command([*cmd,'-frames:v','1','-pix_fmt','yuvj420p',str(temp)])
            if not temp.is_file() or not temp.stat().st_size:raise ValueError('No real frame could be extracted at that time')
            temp.replace(to)
        elif action=='extract_audio':
            if to.suffix!='.wav':raise ValueError('Audio output must be WAV')
            media.command([config.FFMPEG,'-v','error','-y','-i',str(p),'-vn','-ar','48000','-ac','2',str(to)])
        else:
            wanted=float(args['duration']);rate=duration/wanted
            if to.suffix!='.mp4' or not .95<=rate<=1.05:raise ValueError('Only MP4 retiming within 5 percent is allowed')
            cmd=[config.FFMPEG,'-v','error','-y','-i',str(p),'-vf',f'setpts=PTS/{rate}','-c:v','libx264']
            if any(s['codec_type']=='audio' for s in info['streams']):cmd+=['-af',f'atempo={rate}','-c:a','aac']
            media.command([*cmd,'-t',str(wanted),str(to)],timeout=240)
        if action=='conform_duration' and digest(p) in job.get('generated_hashes',[]):record_generated(jid,to)
        return {'path':str(to.relative_to(root))}
    if action=='set_plan':
        segs=args.get('segments',[])
        if not segs or len(segs)>50:raise ValueError('Plan requires 1–50 segments')
        total=sum(float(s['duration']) for s in segs)
        if any(not math.isfinite(float(s['duration'])) or float(s['duration'])<=0 for s in segs):raise ValueError('Invalid segment duration')
        units=args.get('units',[])
        if total>15 and not units:raise ValueError('Long video requires units in set_plan: [{id,duration<=15,reference_range:[start,end] if reference,continuity:{type:independent|same_action|new_angle|new_scene,reason}}]; durations must sum to total')
        if units:
            if any(not 0<float(u['duration'])<=15 or not u.get('id') for u in units) or abs(sum(float(u['duration']) for u in units)-total)>.04:raise ValueError('Generation units must cover total duration, each <=15 seconds')
            if len({u['id'] for u in units})!=len(units):raise ValueError('Unit IDs must be unique')
            for u in units:
                continuity=u.get('continuity',{})
                if continuity.get('type') not in ['independent','same_action','new_angle','new_scene'] or not continuity.get('reason'):raise ValueError('Each unit needs an explicit continuity type and reason')
                from .creative_brief import strict_reference
                if strict_reference(job):
                    r=u.get('reference_range',[])
                    if len(r)!=2 or not 0<=float(r[0])<float(r[1])<=store.asset(job['reference'])['duration']+.05 or r[1]-r[0]>15.05:raise ValueError('Each referenced unit requires a valid <=15 second reference_range')
        args.setdefault('style','');args.setdefault('warnings',[])
        if job.get('production_version') == 2:
            from .unit_planner import validate
            validate(job,root,args)
        store.reserve_plan(jid,args,total,daily_limit=int(os.getenv('VIDEO_AGENT_DAILY_SECONDS','600')) if job.get('owner')!='local' else 0)
        (root/'plan.json').write_text(json.dumps(args,ensure_ascii=False,indent=2))
        return {'saved':True,'duration':total}
    if action=='generate_subject':
        from . import image_asset
        path=image_asset.create_shared_subject(root,str(args['prompt'])[:3000],job['ratio'])
        return {'path':str(path.relative_to(root)),'next':'observe this image before referencing it in generation_source'}
    if action=='generation_source':
        manifest=production.prepare_manifest(job,root,args) if job.get('production_version')==2 else None
        name=args['name']
        if (root/f'{name}.svrun').exists() or (root/f'{name}.svml').exists():
            raise ValueError('工程文件名已存在，请为新修订使用新名称；已有任务使用原Build继续')
        if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,50}',name):raise ValueError('name must be an ASCII filename stem')
        seconds=args['duration']
        if type(seconds)!=int or not 4<=seconds<=15:raise ValueError('Generation duration must be integer 4..15; ceil then trim if target is fractional')
        refs=[];definitions=[]
        images=args.get('images',[]);videos=args.get('videos',[])
        if len(images)>9 or len(videos)>3:raise ValueError('Too many generation references')
        prepared_videos=[]
        for v in videos:
            original=safe(root,v);info=media.probe(original);length=float(info['format']['duration'])
            if length<2:
                adapted=root/'assets'/'adapted'/f'{digest(original)}-2s.mp4';adapted.parent.mkdir(exist_ok=True)
                if not adapted.exists():
                    cmd=[config.FFMPEG,'-v','error','-y','-i',str(original),'-vf','tpad=stop_mode=clone:stop_duration=2']
                    if any(s.get('codec_type')=='audio' for s in info['streams']):cmd+=['-af','apad','-c:a','aac']
                    media.command([*cmd,'-t','2','-c:v','libx264',str(adapted)])
                prepared_videos.append(str(adapted.relative_to(root)))
            else:prepared_videos.append(v)
        videos=prepared_videos
        if sum(float(media.probe(safe(root,v))['format']['duration']) for v in videos)>15.05:raise ValueError('Combined video reference duration must be <=15 seconds')
        if (images or videos) and (args.get('first_frame') or args.get('last_frame')):raise ValueError('Choose reference mode or first/last-frame mode')
        for kind,paths in [('Image',images),('Video',videos)]:
            for i,namepath in enumerate(paths):
                path=safe(root,namepath)
                if not path.is_file():raise ValueError('Reference does not exist: '+namepath)
                if kind=='Video' and float(media.probe(path)['format']['duration'])>15.05:raise ValueError('Cut video reference to <=15 seconds first')
                if kind=='Image' and path.suffix.lower() not in ['.jpg','.jpeg','.png']:raise ValueError('Image reference must be JPG or PNG')
                ref=f'{kind.lower()}{i}'
                definitions.append(f'<asset:{kind} id="{ref}" src="./{compiler.xml(namepath)}"/>')
                refs.append(f'<seedance:Reference {kind.lower()}={{{ref}}}/>')
        frameprops=''
        for key,attr in [('first_frame','first-frame'),('last_frame','last-frame')]:
            if args.get(key):
                path=safe(root,args[key])
                if not path.is_file():raise ValueError('Frame does not exist')
                definitions.append(f'<asset:Image id="{key}" src="./{compiler.xml(args[key])}"/>')
                frameprops+=f' {attr}={{{key}}}'
        if args.get('last_frame') and not args.get('first_frame'):raise ValueError('Last frame requires first frame')
        component='ReferenceVideo' if refs else 'FrameVideo' if frameprops else 'TextVideo'
        props=f'id="clip" model="standard" prompt={{direction}} duration="{seconds}" resolution="720p" aspect-ratio="{job["ratio"]}" generate-audio="true"{frameprops}'
        source=['<?svml using="@hypit/markup@1"?>','<svml>','<import as="asset" from="@hypit/media@1"/>','<import as="text" from="@hypit/text@1"/>','<import as="seedance" from="@hypit/seedance@1"/>',*definitions,f'<text:Value id="direction">{compiler.xml(args["prompt"])}</text:Value>',f'<seedance:{component} {props}>',*refs,f'</seedance:{component}>','</svml>']
        (root/f'{name}.svml').write_text('\n'.join(source))
        (root/f'{name}.svrun').write_text(f'<?svml using="@hypit/run-markup@1"?>\n<svrun version="1"><author source="./{name}.svml"/><target output="clip.video"/></svrun>')
        if manifest:production.register(job,root,manifest,f'{name}.svrun','clip.video')
        return {'run':f'{name}.svrun','output':'clip.video','duration':seconds,'next':'build using these exact run and output values; build includes validation, no separate check needed'}
    if action=='check':return validate_project(root,safe(root,args['run']))
    if action=='build':
        if not job.get('plan'):raise ValueError('Call set_plan before building')
        run=safe(root,args['run']);validate_project(root,run)
        import xml.etree.ElementTree as ET
        targets=[el.get('output') for el in ET.fromstring(run.read_text()).iter('target')]
        if args['output'] not in targets:raise ValueError(f'output must match an actual run target: {targets}')
        profile=hypit.setup(root)
        planned=hypit.plan(root,run,profile)
        sources=source_dependencies(root,run)
        stamp=build_stamp(root,args['run'],args['output'])
        builds=job.get('agent_builds',{})
        if stamp in builds:return builds[stamp]
        # Reserve generation seconds before any submission, including failure recovery.
        seconds=generation_seconds(sources)
        manifest=production.gate_build(job,root,args['run'],args['output']) if seconds and job.get('production_version')==2 else None
        if not seconds and job.get('production_version')==2:
            composition=job.get('composition') or {}
            if composition.get('source_sha256')!=digest(run.with_suffix('.svml')):
                raise ValueError('合成工程已改变，须重新通过 compose 校验')
            production.validate_composition(job,root,composition['clips'])
            if composition.get('sources')!={str(p.relative_to(root)):digest(p) for p in sources}:
                raise ValueError('合成素材或样式已变更，请重新 compose 并审查')
        used=job.get('reserved_generation_seconds',0)
        from . import plan_budget,media_preflight
        budget=plan_budget.job_limit(job)
        if seconds and used+seconds>budget:raise ValueError(f'生成预算不足：已预留 {used} 秒，上限 {budget} 秒')
        if job.get('agent_submission_intent') or job.get('submission_uncertain'):raise ValueError('上次提交回执不确定，需核对，不重复付费')
        profile=hypit.snapshot(root,profile)
        if seconds and job.get('workflow_version',0)>=4:
            store.ensure_plan_budget(jid)
            checked=media_preflight.verify(root,sources,profile)
            if checked:store.update(jid,media_preflight=checked)
        record={'run':args['run'],'output':args['output'],'targets':targets,'state':'pending','generation_seconds':seconds,
                'contains_generated':any(digest(p) in job.get('generated_hashes',[]) for p in sources if p.suffix=='.mp4')}
        record['runtime_profile']=str(profile.relative_to(root))
        record['stamp_version']=2
        if manifest:
            record.update(unit_id=manifest['unit_id'],manifest_id=manifest['version'])
            if job.get('workflow_version',0)>=4:
                from . import failed_generation
                prior=failed_generation.latest(job,manifest['unit_id'])
                grant=failed_generation.authorization(job,prior) if prior and prior.get('state')=='failed' else None
                if grant:
                    if grant['token'] not in manifest['args']['prompt']:raise ValueError('生成工程未绑定新版本授权')
                    record.update(retry_token=grant['token'],retry_of=prior['build_id'])
        if not store.reserve_submission(jid,{'stamp':stamp,'run':args['run'],'output':args['output'],'created':time.time(),'record':record},seconds,budget):
            return {'state':'waiting_capacity','message':'生成并发额度已占满，请立即结束当前回合。后台会自动继续，不要重新准备工程或重复调用。'}
        accepted=hypit.build(root,run,profile);bid=accepted.get('build',{}).get('id')
        if not bid:raise RuntimeError('Hypit 未返回 Build ID')
        record['build_id']=bid
        builds[stamp]=record
        store.update(jid,agent_builds=builds,agent_submission_intent=None)
        return {**record,'next':'End this turn. Supervisor will resume after Build finishes; do not poll repeatedly.'}
    if action in ['status','collect']:
        record=next((b for b in job.get('agent_builds',{}).values() if b['build_id']==args['build_id']),None)
        if not record:raise ValueError('Build does not belong to this task')
        if action=='status':return hypit.status(root,record['build_id'],root/record['runtime_profile'] if record.get('runtime_profile') else hypit.setup(root))
        target=output_path(root,args['to'],job);target.parent.mkdir(parents=True,exist_ok=True)
        if target.suffix!='.mp4':raise ValueError('Video collection needs .mp4')
        # Older runs could record a model-invented output name. Recover only from
        # the actual immutable Build receipt, never by guessing or resubmitting.
        actual_targets=record.get('targets')
        if actual_targets is None:
            actual_targets=hypit.status(root,record['build_id'],hypit.setup(root)).get('build',{}).get('targets',[])
        if actual_targets and record['output'] not in actual_targets:
            if len(actual_targets)!=1:raise ValueError(f'Build output is ambiguous: {actual_targets}')
            record.update(output=actual_targets[0],targets=actual_targets)
            store.update(jid,agent_builds=job['agent_builds'])
        if record.get('remote_result_url') and record.get('provider_recovery'):
            from .provider_recovery import download
            download(record['remote_result_url'],target)
        else:
            temporary=target.with_name(target.stem+'.download.mp4')
            hypit.get(root,record['build_id'],record['output'],temporary)
            if not temporary.is_file() or not temporary.stat().st_size:raise ValueError('下载未得到完整视频文件')
            temporary.replace(target)
        record.update(collected_path=str(target.relative_to(root)),collected_sha256=digest(target))
        store.update(jid,agent_builds=job['agent_builds'])
        if record.get('generation_seconds',0)>0 or record.get('contains_generated'):record_generated(jid,target)
        return {'path':str(target.relative_to(root)),'metadata':media.probe(target)['format']}
    if action=='compose':
        clips=args['clips'];durations=[];paths=[];has_audio=[]
        if job.get('production_version')==2:production.validate_composition(job,root,clips)
        if not clips:raise ValueError('No clips to compose')
        total=sum(float(c['duration']) for c in clips)
        if job.get('duration') is None or not ps.valid_time(job,total):raise ValueError('Composition must cover the complete planned duration; do not omit units')
        for i,c in enumerate(clips):
            p=safe(root,c['path']);d=float(c['duration']);info=media.probe(p);actual=float(info['format']['duration'])
            if not 0<d<=actual+.05:raise ValueError('Clip timeline exceeds actual video duration')
            dest=root/'assets'/'composition'/f'{digest(p)}.mp4';dest.parent.mkdir(exist_ok=True)
            if not dest.exists():copy2(p,dest)
            paths.append(str(dest.relative_to(root)))
            has_audio.append(any(s.get('codec_type')=='audio' for s in info['streams']))
            durations.append(d)
        (root/'look.svs').write_text('<?svml using="@hypit/svs@1"?>\n<sheet version="1">film.main { background: #101010; } media.full { fit: contain; stack-order: 10; playback: once-start; }</sheet>')
        audio=args.get('audio')
        if args.get('mute') and audio:raise ValueError('Choose mute or a soundtrack')
        if args.get('mute'):has_audio=[False]*len(clips)
        if audio and not safe(root,audio).is_file():raise ValueError('audio must be an existing relative audio file path')
        run=compiler.film_source(job,{'segments':clips},root,durations,audio_path=audio,clip_paths=paths,has_audio=has_audio)
        store.update(jid,composition={'clips':[{**c,'compiled_path':p} for c,p in zip(clips,paths)],'boundaries':[sum(durations[:i]) for i in range(1,len(durations))],'audio':audio,'source_sha256':digest(run.with_suffix('.svml')),
            'sources':{str(p.relative_to(root)):digest(p) for p in source_dependencies(root,run)}})
        return {'run':str(run.relative_to(root)),'output':'final.video','duration':sum(durations)}
    if action=='review':
        p=safe(root,args['path']);duration=float(media.probe(p)['format']['duration'])
        from .review_policy import REVIEW_VERSION
        old=job.get('final_review') or {}
        if (old.get('review_version')==REVIEW_VERSION and old.get('verdict') in ('pass','fail') and
            old.get('sha256')==digest(p) and old.get('context_sha256')==review_context(job)):
            return old
        copy2(p,root.parent/'candidate.mp4')
        observations=[]
        for start in range(0,math.ceil(duration),30):
            observations.append(inspect_video(root,p,{'start':start,'end':min(duration,start+30)},'审查实际成片：记录主体外观、事件、镜头、动作、字幕和声音问题。'))
        notes=[json.loads(q.read_text()) for q in (root/'evidence').glob('*-analysis.json') if json.loads(q.read_text()).get('source')=='assets/reference.mp4']
        if job.get('production_version')==2 and job.get('reference_analysis'):
            notes=[{**i['observation'],'visual':i['verification']} for i in job['reference_analysis']['intervals']]
        timeline=[];start_at=0
        for clip in (job.get('composition') or {}).get('clips',[]):
            timeline.append({'unit_id':clip.get('unit_id'),'start':start_at,'end':start_at+float(clip['duration'])});start_at+=float(clip['duration'])
        content=[{'type':'text','text':json.dumps({'goal':job.get('effective_prompt',job['prompt']), 'plan':job.get('plan'),'observations':observations,'timeline':timeline,'reference_notes':notes,'creative_brief':job.get('creative_brief'),'required_events':job.get('reference_analysis',{}).get('events',[]) if creative_brief.strict_reference(job) else []},ensure_ascii=False)}]
        for aid in job['images']:
            content.extend([{'type':'text','text':'以下是用户基准图片。只用于对比成片是否满足其主体、场景或风格用途；审片对象是生成视频，不是这张图片。'},planner.image_content(root/'assets'/f'{aid}.jpg')])
        if job.get('reference'):
            reference=root/'assets'/'reference.mp4'
            for t,frame in media.frames(reference,root/'evidence'/f'{digest(reference)[:16]}-comparison',min(16,max(6,math.ceil(duration)))):
                content.extend([{'type':'text','text':f'原始参考视频 {t} 秒，观察记录可能有误，按实际画面比较'},planner.image_content(frame)])
        for t,frame in media.frames(p,root/'evidence'/f'{digest(p)[:16]}-review',min(16,max(6,math.ceil(duration)))):
            content.extend([{'type':'text','text':f'待审生成视频 {t} 秒'},planner.image_content(frame)])
        # Whole-film judgment and seam checks read the same completed video independently.
        with ThreadPoolExecutor(max_workers=2) as pool:
            joins_future=pool.submit(review_joins,root,p,job)
            instruction='独立审片，检查对象是生成视频。先从原始用户要求中提取可观察的条件，再逐项描述实际画面证据，最后判定；不能把制作方案或目标描述当作已实现的事实。核对主体身份、物体位置与接触关系、动作顺序、镜头变化、文字和声音。空间关系按其实际含义检查：放置在台面上要求主体接触承托面，悬在台面上方不能算放置成功；握住、贴住、戴上等也不能只因两物同时出现就算满足。观察记录指出悬浮、遗漏或错误时，不得在汇总中改写成完成。参考观察的不确定推断不得升级成硬性要求，例如无法确认精确转角时不要强制180度。不得虚构用户未提出的限制：没有台词不等于禁止配乐；未要求静音时合适背景音乐允许。用户明确要求优先于自动方案。任何必要条件明确不符则fail，无法观察则warn，全部满足才pass。JSON：checks数组（每项requirement、evidence含实际时间与画面事实、status为pass/fail/warn、unit_ids为能根据timeline明确归属的生成单元ID数组；无法归属时用空数组，不得猜测），verdict、issues数组、summary。不重复评估基准图。'
            if creative_brief.enabled(job):
                instruction+=' creative_brief为统一验收要求，逐项检查requirements；style/adapt不要求复制未采用的原片事件。每项check增加requirement_ids数组，合计覆盖全部制作要求ID。'
            def validate_review(value):
                if value.get('verdict') not in ('pass','fail','warn') or not isinstance(value.get('issues'),list) or not isinstance(value.get('summary'),str):
                    raise ValueError('检查结论需要verdict、issues数组和summary')
                if job.get('production_version')==2:ps.judge(value)
                creative_brief.validate_review(job,value)
                return value
            review=structured.call(root,'final-review',instruction,content,validate_review,inspection=True)
            joins=joins_future.result()
        if review.get('verdict') not in ['pass','fail','warn'] or not isinstance(review.get('issues'),list) or not isinstance(review.get('summary'),str):raise ValueError('Invalid review')
        if job.get('production_version')==2:review['verdict']=ps.judge(review)
        checks=review.get('checks',[])
        if any(c.get('status')=='fail' for c in checks):review['verdict']='fail'
        elif review['verdict']=='pass' and any(c.get('status')=='warn' for c in checks):review['verdict']='warn'
        original_verdict=review['verdict'];incomplete=[]
        review['joins']=joins
        if any(r['verdict']=='fail' for r in joins):review['verdict']='fail'
        elif review['verdict']=='pass' and any(r['verdict']=='warn' for r in joins):review['verdict']='warn'
        for r in joins:
            review['issues'].extend(f'{r["at"]:.2f}秒衔接：{issue}' for issue in r['issues'])
        if duration>15.2 and not job.get('composition'):
            incomplete.append('长视频缺少生成单元合成记录，无法检查所有衔接点')
        if any(not o['audio']['checked'] and o['audio']['reason']!='no audio track' for o in observations+notes):
            incomplete.append('成片或参考音轨检查未成功，需要复核')
        if job.get('reference') and not reference_coverage(notes,store.asset(job['reference'])['duration']):
            incomplete.append('参考视频尚未完整分析，无法判断复刻效果')
        if incomplete:
            if review['verdict']=='pass':review['verdict']='warn'
            review.setdefault('issues',[]).extend(incomplete)
        if review['verdict']!=original_verdict:
            review['summary']=('未通过：' if review['verdict']=='fail' else '需要复核：')+'；'.join(review['issues'])
        from .review_policy import reconcile
        review=reconcile(review,observations)
        review.update(sha256=digest(p),context_sha256=review_context(job),path=args['path'])
        record=ps.artifact(root,'final-reviews',review)
        store.update(jid,final_review=record,final_review_history=[*job.get('final_review_history',[]),record['record_path']])
        (root/'review.json').write_text(json.dumps(review,ensure_ascii=False,indent=2))
        return review
    if action=='finish':
        p=safe(root,args['path']);review=job.get('final_review') or {}
        if review.get('sha256')!=digest(p) or review.get('context_sha256')!=review_context(job) or review.get('verdict')!='pass':raise ValueError('必须先按当前需求和方案检查当前视频版本并通过；有问题先修复，不能直接完成')
        if not any(b.get('generation_seconds',0)>0 and b.get('state')=='complete' for b in job.get('agent_builds',{}).values()):raise ValueError('No successful generated content; cannot hand back the uploaded reference as a new video')
        if digest(p) not in job.get('generated_hashes',[]):raise ValueError('Output must come from a recorded generated result or its composition/trim; cannot deliver the original reference')
        if job.get('duration') is None:raise ValueError('先通过 set_plan 确定目标时长')
        if not ps.valid_time(job,float(media.probe(p)['format']['duration'])):raise ValueError('成片不符合时长策略')
        if job.get('production_version')==2:production.validate_composition(job,root,job['composition']['clips'])
        media.verify(p,float(media.probe(p)['format']['duration']) if job.get('timing_policy',{}).get('mode')=='approximate' else job['duration']);target=root.parent/'final.mp4';copy2(p,target)
        store.update(jid,status='completed',video=str(target),error=None)
        return {'completed':True,'download':f'/api/jobs/{jid}/video'}
    if action=='ask':
        question=str(args['question'])[:1000]
        store.update(jid,status='needs_input',question=question)
        return {'waiting_for_user':True,'question':question}
    raise ValueError('Unknown action')

if __name__=='__main__':
    try:
        result=dispatch(sys.argv[1],sys.argv[2],json.load(sys.stdin))
        print(json.dumps(result,ensure_ascii=False))
    except Exception as exc:
        # Provider bodies may contain input URLs; do not echo credentials or signed URLs.
        message=re.sub(r'https?://\S+','[url]',str(exc))
        print(json.dumps({'error':message[-2000:]},ensure_ascii=False));sys.exit(1)
