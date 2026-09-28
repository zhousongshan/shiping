"""A new paid revision requires a read-back of a terminal provider task.

A local error or an HTTP 500 is never proof that a remote task failed.
"""
import time
from . import config,store,provider_recovery
from .production_schema import context_version

PAUSED=('failed','needs_attention','needs_configuration','needs_revision')
TERMINAL=('FAILED','CANCELED','CANCELLED')


def latest(job,unit_id):
    builds=[b for b in job.get('agent_builds',{}).values()
            if b.get('generation_seconds',0)>0 and str(b.get('unit_id'))==str(unit_id)]
    return builds[-1] if builds else None


def authorization(job,build):
    grant=job.get('failed_generation_retries',{}).get(build.get('build_id'))
    if (not grant or grant.get('consumed_at') or grant.get('context_version')!=context_version(job)
            or not build.get('terminal_verified')):return None
    return grant


def require_authorization(job,unit_id):
    build=latest(job,unit_id)
    if build and build.get('state')=='failed' and not authorization(job,build):
        raise ValueError('该片段生成失败，请先核对服务商终态并明确创建新版本，不能直接重复生成')


def candidates(job):
    if job.get('workflow_version',0)<4 or job.get('status') not in PAUSED:return []
    if any(job.get(k) for k in ('submission_intent','agent_submission_intent','submission_uncertain')):return []
    result=[]
    for spec in (job.get('plan') or {}).get('units',[]):
        build=latest(job,spec['id'])
        if build and build.get('state')=='failed' and build.get('provider_task_id') and build.get('runtime_profile'):
            result.append({'build_id':build['build_id'],'unit_id':spec['id']})
    return result


def verify(job,build_id):
    if not any(c['build_id']==build_id for c in candidates(job)):
        raise ValueError('没有可核对的失败任务编号，未受理确认的请求不能重新生成')
    build=next(b for b in job['agent_builds'].values() if b['build_id']==build_id)
    root=config.DATA/'jobs'/job['id']/'project'
    profile=(root/build['runtime_profile']).resolve()
    if not profile.is_relative_to(root.resolve()) or not profile.is_file():raise ValueError('缺少原供应商配置凭据')
    result=provider_recovery.poll(root,profile,build['provider_task_id'])
    if result['status'] not in TERMINAL:
        raise ValueError('供应商原任务未确认失败，请继续查询或收集原任务，不重新生成')
    return {'build_id':build_id,'provider_task_id':build['provider_task_id'],
            'status':result['status'],'checked_at':time.time()}
