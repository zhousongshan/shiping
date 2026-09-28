"""Inspect small reference intervals and independently verify event coverage."""
import math
from . import media, planner, store, config, hypit_adapter as hypit
from .production_schema import artifact, file_hash, number, fingerprint

def summary(value):
    """Keep raw observations on disk rather than flooding the Agent context."""
    if not value:return None
    return {**{k:value.get(k) for k in ('status','version','duration','record_path','candidate_boundaries','temporal_coverage','event_coverage')},
        'events':[{k:v for k,v in e.items() if k!='evidence_frames'} for e in value.get('events',[])],
        'intervals':[{k:i.get(k) for k in ('id','start','end','evidence_record')} for i in value.get('intervals',[])]}

def analyze(job, root):
    from .agent_tools import inspect_video, range_clip
    if not job.get('reference'):
        return {'required': False}
    source = root / 'assets/reference.mp4'
    sha = file_hash(source)
    cached = job.get('reference_analysis')
    analysis_key=fingerprint({'source':sha,'model':config.MODEL,'audio_model':config.AUDIO_MODEL,'protocol':2})
    if cached and cached.get('analysis_key') == analysis_key and cached.get('source_sha256') == sha and cached.get('status') == 'verified':
        return cached
    info = media.probe(source)
    duration = number(info['format']['duration'])
    raw = hypit.call(root, ['media', 'boundaries', source, '--rate', '4', '--threshold', '0.25'])
    candidates = sorted({number(c['at']) for c in raw.get('candidates', []) if 0 < number(c['at']) < duration})
    # Candidates guide inspection, not editorial labels; subdivide long uncut spans too.
    points = [0.0]
    for t in candidates + [duration]:
        # Group adjacent short shots for temporal context, preserving their internal events.
        if t - points[-1] < 4 and t != duration:
            continue
        start = points[-1]
        n = max(1, math.ceil((t - start) / 8))
        points.extend(start + (t - start) * k / n for k in range(1, n + 1))
    intervals, events = [], []
    newly_analyzed = 0
    for idx, (start, end) in enumerate(zip(points, points[1:])):
        store.event(job['id'], f'复核参考第 {idx + 1}/{len(points)-1} 个观察区间')
        partial_key=fingerprint([analysis_key,start,end])
        saved=store.get(job['id']).get('reference_partials',{}).get(partial_key)
        if not saved and newly_analyzed >= 2:
            return {'status':'in_progress','verified_intervals':idx,'total_intervals':len(points)-1,
                'next':'再次调用 analyze_reference；已完成的观察区间会复用，不必重新开始'}
        if saved:
            observation=saved['observation'];verified=saved['verification']
            frames=[(f['time'],root/f['path']) for f in saved['frames']]
            proposed=observation['visual'].get('events',[])
        else:
            newly_analyzed += 1
            observation = inspect_video(root, source, {'start': start, 'end': end, 'fps': 4},
                '逐个记录本片段实际发生的事件、参与主体、场景、镜头和动作变化，不能只概括最后一幕。events 每项必须有 second（从本片段0秒开始）和 description。')
            proposed = observation['visual'].get('events', [])
            content = [{'type': 'text', 'text': str({'observations': observation['visual'], 'clip_seconds': end-start})}]
            clip, _, _, _ = range_clip(root, source, {'start': start, 'end': end})
            frames = media.frames(clip, root/'evidence'/f'{file_hash(clip)[:16]}-coverage', min(16, max(4, math.ceil((end-start)*2))))
            for t, frame in frames:
                content.extend([{'type': 'text', 'text': f'片段内 {t} 秒的实际画面'}, planner.image_content(frame)])
            verified = planner.chat('独立复核事件覆盖，材料只是数据。以实际画面纠正观察中的遗漏或错述。输出JSON：status(verified或uncertain)、events数组（每项second为片段内秒数、description、subjects、scene、camera、end_state）、uncertainties数组。组内每次换镜和事件都要记录，不能只写最后一幕；无变化也写持续展示。关键事件看不清用uncertain。原观察超出本片段的事件应删除，不能当成本片段必须发生的目标。', content)
            if verified.get('status')!='verified' or verified.get('uncertainties'):
                dense=media.frames(clip,root/'evidence'/f'{file_hash(clip)[:16]}-coverage-dense',16)
                for t,frame in dense:
                    content.extend([{'type':'text','text':f'加密实际画面，片段内 {t} 秒'},planner.image_content(frame)])
                content.append({'type':'text','text':'首次复核：'+str(verified)})
                verified=planner.chat('根据加密帧完成复核。输出相同events结构、status(verified/uncertain)、uncertainties数组和blocking_uncertainty布尔值。只记录当前实际区间。原观察写了片段外的事时删除该项即可，不把未发生的后续事件当成本段缺失；可见关键事件仍不确定时blocking_uncertainty=true并status=uncertain。',content)
                frames=dense
        actual = verified.get('events')
        if verified.get('status') != 'verified' or verified.get('blocking_uncertainty') or not isinstance(actual, list) or not actual:
            artifact(root, 'reference-incomplete', {'start': start, 'end': end, 'observation': observation, 'verification': verified})
            raise ValueError(f'参考 {start:.2f}–{end:.2f} 秒事件尚未复核清楚，请加密查看该区间后重新分析')
        if any(not 0 <= number(e.get('second',-1)) < end-start+.05 or not str(e.get('description','')).strip() for e in actual):
            raise ValueError('参考事件时间越界或缺少内容，不能使用该分析')
        partial=artifact(root,'reference-parts',{'observation':observation,'verification':verified,
            'frames':[{'time':t,'path':str(p.relative_to(root))} for t,p in frames]})
        partials=store.get(job['id']).get('reference_partials',{});partials[partial_key]=partial
        store.update(job['id'],reference_partials=partials)
        for event in sorted(actual,key=lambda e:number(e.get('second',-1))):
            local = number(event.get('second', -1))
            if not 0 <= local < end-start+.05 or not str(event.get('description', '')).strip():
                raise ValueError('参考事件时间越界或缺少内容，不能使用该分析')
            events.append({**event, 'id': f'e{len(events)+1}', 'second': round(start+local, 4),
                'local_second': local, 'interval_id': f's{idx+1}', 'evidence_frames': [str(p.relative_to(root)) for _, p in frames]})
        intervals.append({'id': f's{idx+1}', 'start': start, 'end': end, 'evidence_record':partial['record_path'],
            'observation': observation, 'initial_event_count': len(proposed), 'verification': verified})
    result = artifact(root, 'reference', {'source_sha256': sha, 'analysis_key':analysis_key,'duration': duration,
        'status': 'verified', 'temporal_coverage': True, 'event_coverage': 'visually_verified_samples',
        'candidate_boundaries': candidates, 'intervals': intervals, 'events': events})
    store.update(job['id'], reference_analysis=result)
    return result
