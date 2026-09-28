"""Execution gates shared by generated and hand-authored Hypit sources."""
import json
from . import store, config, hypit_adapter as hypit, creative_brief
from .production_schema import artifact, context_version, file_hash, fingerprint, number, valid_time
from .unit_planner import unit
from .continuity import validate_inputs, accepted

# This adapter revision changes only transient poll error handling. Its wire
# request and generation capability are unchanged, so accepted receipts and
# existing manifests remain valid across the local recovery fix.
POLL_TRANSPORT_COMPATIBLE = {
    ('aae0d64c7f018878f8addd4996b9e05daeb12946778357e4ce804372e06a11c9',
     '3d2871740b50e70307ca02db7be3d66287ebb508ace2bccbb5dae9446be75245')
}

def unit_generation_limit(job):
    value=job.get('max_unit_generations',3)
    if type(value) is not int or not 1<=value<=3:
        raise ValueError('片段生成次数上限必须为1至3的整数')
    return value

def provider_compatible(saved, actual):
    if saved == actual:
        return True
    if not saved or not actual or (saved.get('adapter_sha256'), actual.get('adapter_sha256')) not in POLL_TRANSPORT_COMPATIBLE:
        return False
    return {k:v for k,v in saved.items() if k!='adapter_sha256'} == {k:v for k,v in actual.items() if k!='adapter_sha256'}

def capabilities(root):
    profile = json.loads(hypit.setup(root).read_text())
    binding = profile['bindings']['@hypit/seedance@1#seedance-2']
    endpoint = profile['endpoints'][binding]
    wan = endpoint['use'] == '@local/provider-autodl-wan'
    adapter = (root/'node_modules/@local/provider-autodl-wan').resolve() if wan else config.ROOT/'provider'
    return {'endpoint': binding, 'module': endpoint['use'], 'base_url': endpoint['config'].get('baseUrl'),
        'model': 'wan3.0-video' if wan else 'seedance-2', 'output_seconds': [4,15],
        'reference_max_seconds': 15, 'max_images': 9, 'max_videos': 3,
        'reference_with_frames': False,'adapter_sha256':fingerprint({p.name:file_hash(p) for p in sorted(adapter.glob('*.js'))}),
        'validation': 'adapter_declared_not_live_verified' if wan else 'historical_samples_only'}

def initialize(job, root):
    profile = capabilities(root)
    if not job.get('provider_profile'):
        store.update(job['id'], provider_profile=profile)
    elif not provider_compatible(job['provider_profile'], profile):
        raise ValueError('任务 Provider 已变化；必须明确建立新版本，不能静默覆盖原执行配置')

def prepare_manifest(job, root, args):
    from .agent_tools import safe
    if not job.get('plan', {}).get('production_version'):
        raise ValueError('旧方案尚未升级：先理解主体、参考事件并重新 set_plan')
    spec = unit(job, args.get('unit_id'))
    if creative_brief.enabled(job):
        from .failed_generation import require_authorization
        require_authorization(job,spec['id'])
    if creative_brief.enabled(job) and job['plan'].get('brief_version')!=job.get('creative_brief',{}).get('version'):
        raise ValueError('创作要求已变化，须重新规划')
    from . import subject_mapping
    if subject_mapping.required(job):
        mapping=subject_mapping.current(job)
        if not mapping or job['plan'].get('subject_mapping_version')!=mapping['version']:
            raise ValueError('先明确主体替换范围并重新规划，不能提交未明确对应的生成')
    if job['plan'].get('subject_version')!=job.get('subject_spec',{}).get('version') or job['plan'].get('reference_version')!=job.get('reference_analysis',{}).get('version'):
        raise ValueError('主体或参考分析已更新，需要重新 set_plan')
    profile = capabilities(root)
    if not provider_compatible(job.get('provider_profile'), profile):
        raise ValueError('Provider 与任务能力快照不一致')
    duration = args.get('duration')
    if type(duration) != int or not profile['output_seconds'][0] <= duration <= profile['output_seconds'][1]:
        raise ValueError('输出时长超出当前适配器能力')
    if duration + .2 < number(spec['duration']):
        raise ValueError('请求时长不足以覆盖计划，请按时长策略调整计划或取整生成')
    if duration-number(spec['duration'])>1.05 and not (duration==4 and number(spec['duration'])<4):
        raise ValueError('生成请求比采用区间长超过1秒，请重新规划而非大量裁弃内容')
    dependencies = validate_inputs(job, root, spec, args)
    images, videos = args.get('images', []), args.get('videos', [])
    if (images or videos) and (args.get('first_frame') or args.get('last_frame')):
        raise ValueError('此接口不支持参考输入与首尾帧同时使用')
    if creative_brief.enabled(job) and not creative_brief.strict_reference(job) and videos:
        raise ValueError('当前借鉴策略不直接提交原视频')
    if creative_brief.strict_reference(job):
        if creative_brief.enabled(job) and not config.media_base_url():raise ValueError('参考素材通道未配置，尚未提交生成')
        if not videos:
            raise ValueError('参考复刻不能遗漏对应视频输入')
        expected = spec['reference_range']
        ranges = []
        for name in videos:
            video = safe(root, name); receipt = video.with_suffix('.cut.json')
            if not receipt.exists():
                raise ValueError('参考片段必须由 cut 准备并保留区间凭据')
            proof = json.loads(receipt.read_text())
            if proof.get('source_sha256') != job['reference_analysis']['source_sha256'] or proof.get('output_sha256') != file_hash(video):
                raise ValueError('参考片段来源或内容不匹配，请重新 cut')
            ranges.append([proof['start'],proof['end']])
        if len(ranges) != 1 or any(abs(a-b) > .05 for a,b in zip(ranges[0], expected)):
            raise ValueError('实际参考区间与当前单元计划不一致')
    supplied = images + videos + [args[k] for k in ('first_frame','last_frame') if args.get(k)]
    assets = [{'path': name, 'sha256': file_hash(safe(root,name))} for name in supplied]
    attempts = [b for b in job.get('agent_builds', {}).values() if b.get('unit_id') == spec['id'] and b.get('generation_seconds',0)]
    if len(attempts) >= unit_generation_limit(job):
        raise ValueError('该单元已达到本任务生成次数上限，结果保留')
    latest = job.get('unit_reviews', {}).get(spec['id'])
    if attempts and attempts[-1].get('state') in ('pending','complete'):
        from .final_repair import dependency_changed
        if not dependency_changed(job,attempts[-1],dependencies) and (not latest or latest.get('verdict') != 'fail' or latest.get('original_sha256',latest.get('sha256')) != attempts[-1].get('collected_sha256') or latest.get('context_version')!=context_version(job)):
            raise ValueError('上一版尚未明确检查失败，请先收集或复核，不重复生成')
    prompt = args.get('prompt', '').strip()
    if len(prompt) < 20:
        raise ValueError('请提供明确的单元导演指令')
    # Stable constraints are serialized into the actual model direction, not just the Agent context.
    events=[{'reference_second':round(e['second']-(spec.get('reference_range') or [0])[0],3),'event':e['description']}
        for e in job.get('reference_analysis',{}).get('events',[]) if e['id'] in spec.get('event_ids',[])]
    directive = '\n'.join([prompt, '本段执行内容：'+spec['description'],
        '统一创作要求（参考事实仅供采用，未采用事件不强制复刻）：'+json.dumps(job.get('creative_brief'),ensure_ascii=False),
        '参考片段事件时序（其中原角色名称只用于对应动作，目标身份以用户图片及替换规则为准）：'+json.dumps(events,ensure_ascii=False),
        '本段需要出现的主体图片：'+json.dumps(spec.get('subject_paths',[]),ensure_ascii=False),
        '主体与素材用途：'+json.dumps(job['subject_spec']['assets'],ensure_ascii=False),
        '替换规则：'+json.dumps(job['subject_spec'].get('replacement_rules',[]),ensure_ascii=False),
        '已确认替换范围（只替换targets，preserve_subjects保留原身份）：'+json.dumps(job.get('subject_mapping'),ensure_ascii=False),
        '对原事件的明确改动：'+json.dumps(spec.get('adaptations',[]),ensure_ascii=False),
        '跨场景身份基准（只借用主体外观，不复用原背景或强制延续动作）：'+json.dumps(spec.get('identity_reason',''),ensure_ascii=False),
        '全片声音方案：'+json.dumps(job['plan'].get('audio_plan',{}),ensure_ascii=False)+'；library模式仅生成对白/环境音，背景配乐由后期统一添加；silent模式不生成声音。',
        '衔接：'+json.dumps(spec['continuity'],ensure_ascii=False)])
    args['prompt'] = directive
    return {'unit_id': spec['id'], 'context_version': context_version(job), 'dependencies': dependencies,
        'inputs': assets, 'provider': profile, 'args': dict(args), 'revision': len(attempts)+1}

def register(job, root, manifest, run, output):
    from .agent_tools import source_dependencies
    root=root.resolve()
    sources = source_dependencies(root, root/run)
    record = artifact(root, 'manifests', {**manifest, 'run': run, 'output': output,
        'sources': {str(p.relative_to(root)): file_hash(p) for p in sources}})
    manifests = dict(store.get(job['id']).get('generation_manifests', {}))
    manifests[record['version']] = record
    store.update(job['id'], generation_manifests=manifests)
    return record

def gate_build(job, root, run, output):
    from .agent_tools import source_dependencies
    root=root.resolve()
    manifests = [m for m in job.get('generation_manifests', {}).values() if m['run'] == run and m['output'] == output]
    current = {str(p.relative_to(root)): file_hash(p) for p in source_dependencies(root, root/run)}
    m = next((m for m in reversed(manifests) if m['sources'] == current and m['context_version'] == context_version(job)), None)
    if not m:
        raise ValueError('生成工程未通过当前 manifest 校验，或工程/素材已修改；请通过 generation_source 重新创建')
    if not provider_compatible(m['provider'], capabilities(root)):
        raise ValueError('实际生成 Provider 已变化')
    if job.get('submission_uncertain'):
        raise ValueError('存在未核对的远端提交，不重复生成')
    if creative_brief.enabled(job):
        from .failed_generation import require_authorization
        require_authorization(job,m['unit_id'])
    attempts=[b for b in job.get('agent_builds',{}).values() if b.get('unit_id')==m['unit_id']]
    if len(attempts)>=unit_generation_limit(job):
        raise ValueError('单元已达到本任务生成次数上限')
    if attempts and attempts[-1].get('state') in ('pending','complete'):
        latest=job.get('unit_reviews',{}).get(m['unit_id'],{})
        from .final_repair import dependency_changed
        if not dependency_changed(job,attempts[-1],m['dependencies']) and (latest.get('verdict')!='fail' or latest.get('original_sha256',latest.get('sha256'))!=attempts[-1].get('collected_sha256') or latest.get('context_version')!=context_version(job)):
            raise ValueError('前版未明确验收失败，不能重复付费提交')
    for dependency, version in m['dependencies'].items():
        if accepted(job, root, dependency)['version'] != version:
            raise ValueError('连续性依赖版本已改变')
    return m

def validate_composition(job, root, clips):
    expected = [str(u['id']) for u in job['plan']['units']]
    if [str(c.get('unit_id')) for c in clips] != expected:
        raise ValueError('正式合成必须按单元顺序覆盖完整计划')
    for c in clips:
        r = accepted(job, root, c['unit_id'])
        if c['path'] != r['path']:
            raise ValueError('合成输入不是该单元已验收版本')
        if abs(number(c['duration']) - r['usable_range'][1]) > .2:
            raise ValueError('裁剪可能删掉已检查事件，需重新规划采用区间并检查')
    if not valid_time(job, sum(number(c['duration']) for c in clips)):
        raise ValueError('合成时长不符合任务时长策略')
