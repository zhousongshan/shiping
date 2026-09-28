"""Validate narrative coverage and real generation-unit dependencies."""
from .production_schema import fingerprint, number, file_hash
from . import creative_brief

RELATIONS = ('independent', 'same_action', 'new_angle', 'new_scene')

def validate(job, root, plan):
    units = plan.get('units')
    if not isinstance(units, list) or not units:
        raise ValueError('每次制作必须提供 units，即使只有一个生成单元')
    ids = [str(u.get('id', '')) for u in units]
    if any(not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('生成单元 ID 必须唯一')
    subjects = job.get('subject_spec')
    if not subjects:
        raise ValueError('先调用 understand_subjects 读取用户图和素材用途')
    from . import subject_mapping
    if subject_mapping.required(job):
        mapping=subject_mapping.current(job)
        if not mapping:raise ValueError('主体替换对应尚未明确，请先确认要替换的参考角色')
        plan['subject_mapping_version']=mapping['version']
    strict=creative_brief.strict_reference(job)
    brief=job.get('creative_brief') or {}
    required={r['id'] for r in brief.get('requirements',[])}
    covered_requirements=set()
    if creative_brief.enabled(job) and not required:raise ValueError('缺少统一创作要求')
    source = job.get('reference_analysis')
    total=sum(number(u.get('duration')) for u in units)
    # Narrative beats and camera cuts do not require separate paid requests.
    # Retain legacy plans; only validate new standard-workflow proposals.
    if (job.get('workflow_version',0)>=3 and total<=15 and len(units)>1
            and (not strict or (source or {}).get('duration',float('inf'))<=15)):
        raise ValueError('15秒以内且参考不超过15秒的短片应使用一个生成单元；多个动作或镜头写入segments和同一unit，不能按叙事小节拆成多个收费请求')
    if job.get('reference'):
        if not source or source.get('status') != 'verified' or source.get('source_sha256') != file_hash(root/'assets/reference.mp4'):
            raise ValueError('先调用 analyze_reference 完成参考事件复核')
    events = {e['id']: e for e in (source or {}).get('events', [])}
    covered = []
    previous_start = -1
    covered_until = 0.0
    for index, u in enumerate(units):
        u['id'] = str(u['id'])
        u.setdefault('depends_on', [])
        identity_paths=[a['path'] for a in subjects['assets'] if a['role']=='identity']
        u.setdefault('subject_paths',identity_paths)
        reference_paths=[a['path'] for a in subjects['assets'] if a['role'] in ('scene','style')]
        u.setdefault('reference_paths',reference_paths)
        if not isinstance(u['reference_paths'],list) or any(p not in reference_paths for p in u['reference_paths']):
            raise ValueError('场景或风格参考必须对应实际上传素材')
        if not isinstance(u['subject_paths'],list) or any(p not in identity_paths for p in u['subject_paths']):
            raise ValueError('单元主体必须对应真实身份图片')
        if identity_paths and not u['subject_paths'] and not u.get('no_subject_reason'):
            raise ValueError('纯场景镜头不带主体时需说明 no_subject_reason')
        u['depends_on'] = [str(d) for d in u['depends_on']]
        if any(d not in ids[:index] for d in u['depends_on']):
            raise ValueError('依赖必须指向前面的单元，不能成环')
        if creative_brief.enabled(job):
            identity_deps=u.setdefault('identity_depends_on',[])
            if not isinstance(identity_deps,list) or any(not isinstance(d,str) or d not in ids[:index] for d in identity_deps):
                raise ValueError('身份基准必须指向前面已规划单元，不能自引用或成环')
            if identity_deps and not u.get('identity_reason'):raise ValueError('绑定身份基准时须说明共享的主体')
        relation = u.get('continuity', {}).get('type')
        if relation not in RELATIONS or not u.get('continuity', {}).get('reason'):
            raise ValueError('缺少有效连续性策略')
        if relation in ('same_action','new_angle') and (index == 0 or ids[index-1] not in u['depends_on']):
            raise ValueError('连续动作或换机位必须依赖前一单元')
        if not isinstance(u.get('description'), str) or not u['description'].strip():
            raise ValueError('每单元必须描述实际事件与动作，不只写一句保持一致')
        if creative_brief.enabled(job):
            ids_for_unit=u.get('requirement_ids')
            if not isinstance(ids_for_unit,list) or any(r not in required for r in ids_for_unit):
                raise ValueError('每段requirement_ids必须来自统一创作要求')
            covered_requirements.update(ids_for_unit)
            if not strict:
                selected=u.get('event_ids',[])
                if not isinstance(selected,list) or any(e not in events for e in selected):raise ValueError('借鉴事件必须来自真实参考观察')
                if u.get('reference_range') is not None:raise ValueError('风格借鉴/改编不直接绑定原视频区间，请按创作要求规划')
        if events and strict:
            selected = u.get('event_ids', [])
            if not selected or any(e not in events for e in selected):
                raise ValueError('每个参考单元必须对应已观察 event_ids')
            start, end = map(number, u.get('reference_range', []))
            if start < previous_start or end <= start:
                raise ValueError('参考单元不能颠倒顺序')
            if start > covered_until+.1 or end > source['duration']+.05:
                raise ValueError('参考区间存在遗漏或越界')
            covered_until=max(covered_until,end)
            previous_start = start
            if any(not start-.1 <= events[e]['second'] <= end+.1 for e in selected):
                raise ValueError('事件不在该单元的参考范围内')
            covered.extend(selected)
            # A split within a camera take needs explicit action context, not equal slicing.
            near_cut = start == 0 or any(abs(start-t) <= .4 for t in source['candidate_boundaries'])
            if not near_cut and not u['continuity'].get('boundary_reason'):
                raise ValueError('非候选切点分段必须说明动作阶段 boundary_reason 并建立连续依赖')
            if not near_cut and relation != 'same_action':
                raise ValueError('镜头内部拆分须采用 same_action，不能假装独立换景')
    if strict and events and (set(covered) != set(events) or len(covered) != len(set(covered))):
        raise ValueError('已观察事件必须各有一个采用单元，不能遗漏或重复播放')
    if strict and events and (covered_until < source['duration']-.1 or [events[e]['second'] for e in covered] != sorted(events[e]['second'] for e in covered)):
        raise ValueError('参考结尾遗漏或事件顺序发生变化')
    if creative_brief.enabled(job):
        if covered_requirements!=required:raise ValueError('计划遗漏创作要求，请补齐requirement_ids覆盖')
        from . import film_audio
        film_audio.validate(plan)
        plan['brief_version']=brief['version']
    plan['production_version'] = 2
    plan['reference_version'] = (source or {}).get('version')
    plan['subject_version'] = subjects['version']
    return fingerprint(plan)

def unit(job, unit_id):
    value = next((u for u in (job.get('plan') or {}).get('units', []) if str(u['id']) == str(unit_id)), None)
    if not value:
        raise ValueError('不存在的 unit_id；先提交生成单元计划')
    return value
