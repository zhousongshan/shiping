"""Bundle mechanical tool steps while preserving the existing execution gates."""
import math

from . import store, continuity
from .production_schema import context_version, file_hash, fingerprint
from .unit_planner import unit


def review_summary(review):
    # The full visual/audio observations remain in the immutable evidence record.
    return {k: v for k, v in review.items() if k != 'observation'}


def generate_unit(job, root, args, execute):
    from .agent_tools import range_clip, safe
    if job.get('production_version') != 2:
        raise ValueError('generate_unit requires a validated v2 plan')
    allowed = {'unit_id', 'prompt', 'duration', 'images', 'first_frame', 'last_frame'}
    if set(args) - allowed:
        raise ValueError('generate_unit只需unit_id、prompt和可选duration/images/first_frame/last_frame；文件名和参考裁切由后台处理')
    if job.get('agent_submission_intent') or job.get('submission_uncertain'):
        raise ValueError('上次提交回执尚未确认，不重复生成')
    spec = unit(job, args.get('unit_id'))
    request = {**args, 'unit_id': spec['id'],
        'duration': args.get('duration', max(4, math.ceil(float(spec['duration'])))),
        'images': list(args.get('images', [*spec.get('subject_paths', []),*spec.get('reference_paths',[])]))}
    dependencies = {}
    for dep in spec.get('depends_on', []):
        review = continuity.accepted(job, root, dep)
        frame = review.get('end_frame')
        if not frame or file_hash(root/frame) != review.get('end_frame_sha256'):
            raise ValueError('缺少经过验收的衔接画面')
        dependencies[str(dep)] = review['version']
        if frame != request.get('first_frame') and frame not in request['images']:
            request['images'].append(frame)
    if job.get('reference'):
        start, end = spec['reference_range']
        clip, _, _, _ = range_clip(root, root/'assets/reference.mp4', {'start': start, 'end': end})
        request['videos'] = [str(clip.relative_to(root))]
    inputs = request['images'] + request.get('videos', []) + [request[k]
        for k in ('first_frame', 'last_frame') if request.get(k)]
    key = fingerprint({'request': request, 'context': context_version(job),
        'dependencies': dependencies, 'inputs': {p: file_hash(safe(root, p)) for p in inputs}})
    # Identical calls reuse the recorded source and Build, including terminal failures.
    # A lost submission response must never be converted into a new paid attempt.
    request['name'] = 'unit_' + key[:24]
    run = request['name'] + '.svrun'
    if (root/run).exists():
        if not any(m['run'] == run for m in job.get('generation_manifests', {}).values()):
            raise ValueError('工程缺少生成记录，请核对后继续')
    else:
        execute(job['id'], 'generation_source', request)
    # build already validates source, provenance, dependencies and budget.
    return execute(job['id'], 'build', {'run': run, 'output': 'clip.video'})


def collect_review(job, root, args, execute):
    build = next((b for b in job.get('agent_builds', {}).values()
        if b['build_id'] == args.get('build_id')), None)
    if not build or not build.get('unit_id'):
        raise ValueError('请选择本任务的生成单元Build')
    if build.get('state') != 'complete':
        raise ValueError('Build尚未完成，请等待后台更新，不重新提交')
    path = build.get('collected_path')
    if not path or not (root/path).is_file() or file_hash(root/path) != build.get('collected_sha256'):
        path = 'generated_' + fingerprint(build['build_id'])[:24] + '.mp4'
        execute(job['id'], 'collect', {'build_id': build['build_id'], 'to': path})
    fresh = store.get(job['id'])
    review = fresh.get('unit_reviews', {}).get(str(build['unit_id']), {})
    if (review.get('verdict') == 'pass' and
            review.get('original_sha256', review.get('sha256')) == file_hash(root/path)):
        # This also validates the current context, adopted clip and dependencies.
        review = continuity.accepted(fresh, root, build['unit_id'])
    else:
        review = execute(job['id'], 'review_unit', {'unit_id': build['unit_id'], 'path': path})
    return review_summary(review)


def compose_build(job, root, args, execute):
    request = dict(args)
    if 'clips' not in request:
        clips = []
        for spec in job['plan']['units']:
            review = continuity.accepted(job, root, spec['id'])
            clips.append({'unit_id': spec['id'], 'path': review['path'],
                'duration': review['usable_range'][1]})
        request['clips'] = clips
    made = execute(job['id'], 'compose', request)
    return execute(job['id'], 'build', {'run': made['run'], 'output': made['output']})
