"""Account for a complete first pass before spending on any one unit.

Units are provider seconds, not a claim about currency or provider balance.
Accepted, failed and uncertain submissions remain charged to the local ledger.
"""
import datetime
import math
import os
import time


class BudgetUnavailable(ValueError):
    pass


def job_limit(job):
    return float(os.getenv('VIDEO_AGENT_JOB_SECONDS', str(max(30, 2*(job.get('duration') or 30)))))


def costs(job):
    profile=job.get('provider_profile') or {}
    minimum,maximum=profile.get('output_seconds',[4,15])
    result={}
    for unit in (job.get('plan') or {}).get('units',[]):
        seconds=max(minimum,math.ceil(float(unit['duration'])))
        if seconds>maximum:raise BudgetUnavailable('计划片段时长超出供应商能力，请重新规划')
        result[str(unit['id'])]=seconds
    return result


def missing(job):
    needed=costs(job)
    for build in job.get('agent_builds',{}).values():
        if build.get('state') in ('complete','pending') and build.get('generation_seconds',0)>0:
            needed.pop(str(build.get('unit_id')),None)
    return needed


def day_and_spent(job,day,today):
    ledger=job.get('generation_daily_reservations')
    return ledger.get(day,0) if ledger is not None else job.get('reserved_generation_seconds',0) if job.get('created',0)>=today else 0


def allocate(job,jobs,*,seconds=0,unit_id=None,limit=None,approval=None):
    """Called while the store write lock is held. Returns the unspent hold."""
    planned=costs(job);remaining=missing(job)
    if unit_id is not None:remaining.pop(str(unit_id),None)
    future=sum(remaining.values())
    required=seconds+future
    used=job.get('reserved_generation_seconds',0)
    cap=job_limit(job) if limit is None else limit
    if used+required>cap:
        raise BudgetUnavailable(f'整片生成预算不足：已预留 {used:g} 秒，本次及剩余片段至少需要 {required:g} 秒，上限 {cap:g} 秒；尚未提交本次生成')
    if approval:
        count=len(remaining)+(1 if seconds else 0)
        if (approval['expires']<=time.time() or used+required>approval['max_seconds']
                or job.get('authorized_submission_count',0)+count>approval['max_submissions']):
            raise BudgetUnavailable('单次测试授权不足以覆盖整片剩余生成，请管理员核对授权')
    date=datetime.datetime.now().astimezone();day=date.date().isoformat()
    today=date.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    owner=job.get('owner','local')
    daily=int(os.getenv('VIDEO_AGENT_DAILY_GENERATION_SECONDS','0' if owner=='local' else '600'))
    same=[j for j in jobs if j.get('owner','local')==owner]
    spent=sum(day_and_spent(j,day,today) for j in same)
    other_holds=sum(j.get('plan_budget',{}).get('remaining_seconds',0) for j in same
        if j['id']!=job['id'] and not j.get('archived_at') and j.get('status')!='completed'
        and j.get('plan_budget',{}).get('day')==day and j.get('plan_budget',{}).get('expires',0)>time.time())
    if daily and spent+other_holds+required>daily:
        raise BudgetUnavailable('今日预留生成预算不足以覆盖整片（包含其他任务、返修和待核对提交），尚未提交本次生成')
    return {'first_pass_seconds':sum(planned.values()),'remaining_seconds':future,
        'spent_seconds':used+seconds,'job_limit_seconds':cap,'day':day,
        'expires':time.time()+3600,'checked_at':time.time(),
        'scope':'local_generation_seconds_not_provider_balance'}
