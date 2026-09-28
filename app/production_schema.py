"""Versioned production facts. Model prose never grants execution authority."""
import hashlib
import json
import math
import os
from pathlib import Path

VERSION = 2

def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def file_hash(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()

def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('时间必须是有限数字')
    return result

def artifact(root, kind, value):
    """Content-addressed evidence; DB indexes are committed only after this returns."""
    value = {**value, 'schema_version': VERSION}
    version = fingerprint(value)
    path = root / 'production' / kind / (version + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        temp = path.with_suffix(f'.{os.getpid()}.tmp')
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        temp.replace(path)
    return {**value, 'version': version, 'record_path': str(path.relative_to(root))}

def valid_time(job, duration):
    duration = number(duration)
    policy = job.get('timing_policy') or {'mode': 'strict'}
    if policy['mode'] == 'approximate':
        return number(policy['min']) <= duration <= number(policy['max'])
    target = job.get('duration')
    return target is None or abs(duration - number(target)) <= .15

def context_version(job):
    fields=['prompt','effective_prompt','images','reference','plan','duration',
            'timing_policy','reference_analysis','subject_spec','provider_profile']
    if job.get('workflow_version',0)>=4:fields.append('creative_brief')
    return fingerprint({k:job.get(k) for k in fields})

def judge(result):
    """Reject empty or contradictory evidence instead of trusting a verdict string."""
    checks = result.get('checks')
    if not isinstance(checks, list) or not checks:
        raise ValueError('检查没有返回可核对的证据')
    if any(not isinstance(c, dict) or c.get('status') not in ('pass', 'fail', 'warn')
           or not isinstance(c.get('evidence'), str) or not c['evidence'].strip() for c in checks):
        raise ValueError('检查项缺少实际证据或有效状态')
    verdict = result.get('verdict')
    if verdict not in ('pass', 'fail', 'warn'):
        raise ValueError('检查结论无效')
    if any(c['status'] == 'fail' for c in checks):
        return 'fail'
    if any(c['status'] == 'warn' for c in checks):
        return 'warn' if verdict == 'pass' else verdict
    return verdict


def audio_evidence(value):
    """A response about pictures is never proof that sound was inspected."""
    fields=('speech','music','effects','uncertainties')
    if not isinstance(value,dict) or any(k not in value for k in fields):
        raise ValueError('音频检查未返回 speech/music/effects/uncertainties 证据')
    if any(not isinstance(value[k],(str,list)) for k in fields):
        raise ValueError('音频证据结构无效')
    if not any(value[k] for k in fields):
        raise ValueError('音频检查返回空证据')
    return value
