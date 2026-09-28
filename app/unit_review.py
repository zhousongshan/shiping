"""Review real generated units before allowing their use as continuity inputs."""
import json
from . import media, planner, store, config
from .production_schema import artifact, context_version, file_hash, judge, number
from .unit_planner import unit
from . import review_policy, structured, creative_brief

def bounded_range(region, duration):
    """Accept a two-decimal rounded endpoint, never a materially longer interval."""
    start,end=number(region['start']),number(region['end'])
    if duration < end <= duration + .005 + 1e-9:
        end=duration
    if not 0 <= start < end <= duration:
        raise ValueError(f'审片疑点区间越界，须满足0 <= start < end <= {duration:.6f}秒')
    return {**region,'start':start,'end':end}

def review(job, root, args):
    from .agent_tools import safe, inspect_video, digest, record_generated
    spec = unit(job, args['unit_id'])
    path = safe(root, args['path'])
    sha = file_hash(path)
    generation = next((b for b in job.get('agent_builds', {}).values()
        if b.get('unit_id') == spec['id'] and b.get('state') == 'complete' and b.get('collected_sha256') == sha), None)
    if not generation:
        raise ValueError('只能验收该单元真实生成并收集的输出')
    manifest = job['generation_manifests'][generation['manifest_id']]
    if manifest['context_version'] != context_version(job):
        raise ValueError('生成上下文已变化，不能按旧计划验收')
    from .continuity import accepted,reference_frame
    for dep, version in manifest['dependencies'].items():
        if accepted(job, root, dep)['version'] != version:
            raise ValueError('前段已修改，当前生成使用了旧衔接画面')
    previous=job.get('unit_reviews',{}).get(spec['id'],{})
    focus=None
    if args.get('final_recheck'):
        from .final_repair import targets
        final=job.get('final_review') or {}
        if final.get('verdict')!='fail' or spec['id'] not in targets(job,final):
            raise ValueError('缺少可定位到该单元的成片失败证据')
        focus={'checks':final.get('checks',[]),'joins':final.get('joins',[]),
               'instruction':'成片审查指出以下可能问题。只根据当前实际片段判断；若无法在片段重现，不得伪造失败来触发重新生成。'}
    if (not focus and previous.get('review_version')==review_policy.REVIEW_VERSION and
        previous.get('original_sha256',previous.get('sha256'))==sha and
        previous.get('context_version')==context_version(job) and
        previous.get('dependencies')==manifest['dependencies'] and
        previous.get('verdict') in ('pass','fail') and
        file_hash(root/previous['path'])==previous['sha256']):
        return previous
    duration = number(media.probe(path)['format']['duration'])
    original_sha = sha
    # Check the actual adopted interval, not a longer take then cut off unreviewed content.
    wanted = number(spec['duration'])
    if job.get('timing_policy', {}).get('mode') != 'approximate' and duration > wanted + .05:
        from .agent_tools import range_clip
        path, _, _, _ = range_clip(root,path,{'start':0,'end':wanted})
        record_generated(job['id'],path)
        duration = number(media.probe(path)['format']['duration']);sha=file_hash(path)
    observation = inspect_video(root, path, {'start': 0, 'end': duration, 'fps': 5},
        '检查整段每次镜头变化时主体脸部、形状、动作、场景与原角色残留，逐项记录实际时间；不把目标当事实。')
    events = [e for e in job.get('reference_analysis', {}).get('events', []) if e['id'] in spec.get('event_ids', [])]
    content = [{'type': 'text', 'text': json.dumps({'user_goal':job.get('effective_prompt',job['prompt']),'user_feedback':job.get('feedback'),'unit': spec, 'subjects': job['subject_spec'], 'reference_events': events,
        'audio_plan':job['plan'].get('audio_plan'), 'actual_observation': observation, 'seconds': duration,'final_recheck':focus,'creative_brief':job.get('creative_brief')}, ensure_ascii=False)}]
    for a in job['subject_spec']['inputs']:
        content += [{'type': 'text', 'text': '用户基准：'+a['path']}, planner.image_content(root/a['path'])]
    frames = media.frames(path, root/'evidence'/f'{sha[:16]}-unit-review', 16)
    for t, frame in frames:
        content += [{'type': 'text', 'text': f'生成片段 {t} 秒'}, planner.image_content(frame)]
    video_stream=next(s for s in media.probe(path)['streams'] if s['codec_type']=='video')
    last_sample=max(0,min(duration,number(video_stream.get('duration',duration)))-.12)
    for at in [0, max(0,duration-.3), last_sample]:
        frame=root/'evidence'/f'{sha[:16]}-endpoint-{at:.4f}.jpg'
        media.command([config.FFMPEG,'-v','error','-y','-ss',str(at),'-i',str(path),'-frames:v','1','-pix_fmt','yuvj420p',str(frame)])
        content += [{'type':'text','text':f'边界实际帧 {at:.4f} 秒'},planner.image_content(frame)]
    for dep in manifest['dependencies']:
        prev = accepted(job, root, dep)
        frame,_=reference_frame(spec,dep,prev)
        identity_only=dep in spec.get('identity_depends_on',[]) and dep not in spec.get('depends_on',[])
        content += [{'type': 'text', 'text': ('跨场景主体外观基准，不要求相同背景或延续动作：' if identity_only else '前段验收结束状态：')+json.dumps(prev.get('identity_state') if identity_only else prev.get('end_state'), ensure_ascii=False)}, planner.image_content(root/frame)]
    if events and creative_brief.strict_reference(job):
        ref = root/'assets/reference.mp4'
        from .agent_tools import range_clip
        clipped, _, _, _ = range_clip(root, ref, {'start': spec['reference_range'][0], 'end': spec['reference_range'][1]})
        for t, frame in media.frames(clipped, root/'evidence'/f'{file_hash(clipped)[:16]}-unit-reference', 8):
            content += [{'type': 'text', 'text': f'对应原参考片段 {t} 秒，仅对照动作和事件'}, planner.image_content(frame)]
    instruction = '独立检查真实生成片段。输出JSON：verdict(pass/fail/warn)、checks（每项category、requirement、status、evidence实际时间和事实，至少包含identity/events/continuity/audio四个category）、issues数组、summary、end_state、end_frame_second、suspect_ranges数组（start/end为片段内秒数）。完整主体替换不能保留原玩偶脸或只是换装。事件必须按对应参考与需求发生。主体、事件、动作、声音分别判断；有明确错误fail，看不清warn，不能单凭流畅或最后画面正确判pass。无音轨或首镜无前镜应明确说明不适用。结束帧只能从已观察且清晰的真实帧选择。'
    if creative_brief.enabled(job):
        instruction+=' 以creative_brief与本unit为验收标准，style/adapt不要求复刻未采用参考事件；只在replacement_required时检查替换。audio_plan.mode=library时片段无需已有背景配乐，全片合成后再验收配乐；片段仍需检查要求的对白与音效。silent模式会在后期移除全部音轨，片段有声本身不算失败，最终成片必须无声。每项check增加requirement_ids数组，列出实际检查的制作要求ID，合计覆盖本unit.requirement_ids。不能把unit.id如u1填入requirement_ids；额外技术或衔接检查无对应用户要求时填[]。'
    needs_identity=any(spec['id'] in u.get('identity_depends_on',[]) for u in job['plan']['units'])
    if needs_identity:
        instruction+=' 本段将作为后续跨场景主体基准。额外返回identity_frame_second与identity_state：从已观察的实际帧中选主体清晰完整、特征可辨的时间，描述其外观；不必在片尾。不能找到明确身份帧则warn，不虚构画面。'
    def validate_result(result):
        verdict=judge(result)
        if not {'identity','events','continuity','audio'} <= {c.get('category') for c in result['checks']}:
            raise ValueError('单元审查遗漏身份、事件、衔接或声音类别')
        if not isinstance(result.get('issues'),list) or not isinstance(result.get('summary'),str):
            raise ValueError('审片需要issues数组和summary文字')
        regions=result.get('suspect_ranges',[])
        if not isinstance(regions,list):raise ValueError('疑点区间须为数组')
        result['suspect_ranges']=[bounded_range(r,duration) for r in regions]
        if verdict=='pass':
            at=number(result.get('end_frame_second',-1))
            if not 0<=at<duration or duration-at>.6 or not result.get('end_state'):
                raise ValueError('通过审查需要最后0.6秒内的实际结束帧与结束状态')
        if verdict=='pass' and needs_identity:
            if not 0<=number(result.get('identity_frame_second',-1))<duration or not result.get('identity_state'):
                raise ValueError('后续主体基准需要实际清晰身份帧与外观记录')
        creative_brief.validate_review(job,result,spec)
        return result
    result=structured.call(root,'unit-review',instruction,content,validate_result,inspection=True)
    verdict = judge(result)
    # Focused reread is allowed before spending on another generation.
    for region in result.get('suspect_ranges', [])[:3]:
        start, end = number(region['start']), number(region['end'])
        if not 0 <= start < end <= duration:
            raise ValueError('审片疑点区间越界')
        from .agent_tools import range_clip
        clip, _, _, _ = range_clip(root, path, {'start': start, 'end': end})
        for t, frame in media.frames(clip, root/'evidence'/f'{file_hash(clip)[:16]}-dense', 12):
            content += [{'type': 'text', 'text': f'疑点加密 {start+t:.2f} 秒'}, planner.image_content(frame)]
    if result.get('suspect_ranges'):
        content += [{'type': 'text', 'text': '前次待复核结果：'+json.dumps(result, ensure_ascii=False)}]
        result = structured.call(root,'unit-review-dense',instruction+' 结合加密实际帧复核，不得无依据推翻已观察错误；无法确认用warn。',content,validate_result,inspection=True)
        verdict = judge(result)
    if not {'identity','events','continuity','audio'} <= {c.get('category') for c in result['checks']}:
        raise ValueError('单元审查遗漏身份、事件、衔接或声音类别')
    if not observation['audio']['checked'] and observation['audio'].get('reason') != 'no audio track' and verdict == 'pass':
        verdict = 'warn'
        result.setdefault('issues', []).append('音轨尚未成功检查')
    result=review_policy.reconcile({**result,'verdict':verdict},[observation]);verdict=result['verdict']
    output = {**result, 'verdict': verdict, 'unit_id': spec['id'], 'path': str(path.relative_to(root)), 'sha256': sha,'original_sha256':original_sha,
        'context_version': context_version(job), 'dependencies': manifest['dependencies'], 'observation': observation}
    if verdict == 'pass':
        at = number(result.get('end_frame_second', -1))
        if not 0 <= at < duration or duration-at > .6:
            raise ValueError('结束状态必须来自最后0.6秒内的清晰实际画面；否则应调整采用区间后复查')
        if not result.get('end_state'):
            raise ValueError('缺少实际结束状态')
        frame = root/'production'/'frames'/f'{sha}-{at:.4f}.jpg'
        frame.parent.mkdir(parents=True, exist_ok=True)
        media.command([config.FFMPEG,'-v','error','-y','-ss',str(at),'-i',str(path),'-frames:v','1','-pix_fmt','yuvj420p',str(frame)])
        output.update(end_frame=str(frame.relative_to(root)), end_frame_sha256=file_hash(frame), usable_range=[0, duration])
    if verdict=='pass' and needs_identity:
        at=number(result['identity_frame_second'])
        frame=root/'production'/'frames'/f'{sha}-identity-{at:.4f}.jpg'
        media.command([config.FFMPEG,'-v','error','-y','-ss',str(at),'-i',str(path),'-frames:v','1','-pix_fmt','yuvj420p',str(frame)])
        output.update(identity_frame=str(frame.relative_to(root)),identity_frame_sha256=file_hash(frame),identity_state=result['identity_state'])
    output = artifact(root, 'unit-reviews', output)
    reviews = dict(job.get('unit_reviews', {}))
    # A focused pass on unchanged media should not invalidate every downstream
    # accepted frame just because its explanatory prose changed.
    if not (focus and verdict=='pass' and previous.get('verdict')=='pass'):
        reviews[spec['id']] = output
    history=job.get('unit_review_history',{})
    history[spec['id']]=[*history.get(spec['id'],[]),output['record_path']]
    store.update(job['id'], unit_reviews=reviews,unit_review_history=history)
    return output
