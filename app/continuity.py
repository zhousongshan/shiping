"""Continuity references must come from accepted, current output versions."""
from .production_schema import context_version, file_hash, fingerprint
from .unit_planner import unit

def accepted(job, root, unit_id, visited=None):
    visited = set(visited or ())
    if str(unit_id) in visited:
        raise ValueError('检测到连续性依赖环')
    visited.add(str(unit_id))
    review = job.get('unit_reviews', {}).get(str(unit_id))
    if not review or review.get('verdict') != 'pass':
        raise ValueError(f'单元 {unit_id} 尚未通过实际检查')
    if review['context_version'] != context_version(job) or file_hash(root/review['path']) != review['sha256']:
        raise ValueError(f'单元 {unit_id} 的验收已过期')
    for dependency, version in review.get('dependencies', {}).items():
        if accepted(job, root, dependency, visited)['version'] != version:
            raise ValueError('依赖片段已经更新，需要重新检查衔接')
    return review

def validate_inputs(job, root, spec, args):
    dependencies = {}
    images = args.get('images', [])
    for asset in job['subject_spec']['inputs']:
        if file_hash(root/asset['path']) != asset['sha256']:
            raise ValueError('用户素材已变化，需要重新理解主体与规划')
    for dependency in spec.get('depends_on', []):
        r = accepted(job, root, dependency)
        dependencies[str(dependency)] = r['version']
        path = r.get('end_frame')
        if not path or file_hash(root/path) != r.get('end_frame_sha256'):
            raise ValueError('缺少经过验收的衔接画面')
        if path not in images and args.get('first_frame') != path:
            raise ValueError('实际请求没有绑定前段验收画面；文字“保持一致”不能替代素材')
    identity = spec.get('subject_paths',[a['path'] for a in job['subject_spec']['assets'] if a['role']=='identity'])
    if any(p not in images and args.get('first_frame') != p for p in identity):
        raise ValueError('实际请求缺少用户主体身份图片')
    if any(p not in images for p in spec.get('reference_paths',[])):
        raise ValueError('实际请求缺少已规划的场景或风格参考图片')
    return dependencies
