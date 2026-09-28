import json, uuid, time, hashlib, os
from .config import DATA
from . import database, execution

LEASE_SECONDS = 90
RUNNING_STATES = ('analyzing','planning','preparing_assets','generating','checking','reviewing','rendering','agent_running','waiting_build')
def connect():
    return database.connect(DATA)
def now(): return time.time()
def track_status(d, status):
    moment=now()
    previous=d.get('stage_clock')
    if previous and previous['status']==status:return
    if previous:
        elapsed=max(0,moment-previous['started'])
        totals=d.setdefault('status_seconds',{})
        totals[previous['status']]=round(totals.get(previous['status'],0)+elapsed,3)
        d['stage_history']=(d.get('stage_history',[])+[{**previous,'ended':moment,'seconds':round(elapsed,3)}])[-300:]
    d['stage_clock']={'status':status,'started':moment}
def put_asset(d):
    with connect() as c:c.execute("INSERT INTO assets VALUES (?,?)",(d["id"],json.dumps(d,ensure_ascii=False)))
def asset(i):
    with connect() as c:r=c.execute("SELECT doc FROM assets WHERE id=?",(i,)).fetchone()
    if not r:raise KeyError("素材不存在")
    return json.loads(r['doc'])
GENERATION_PAUSED = '存在多条待核对的供应商提交，已暂停新增收费生成；请管理员核对回执后继续'

def generation_request_digest(d):
    return hashlib.sha256(json.dumps({k:v for k,v in d.items() if k not in ('source_version','workflow_version')},sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def authorize_generation_request(owner,request_key,request_doc,*,max_seconds,max_submissions,note):
    """Local operator API only: call after explicit, scoped user authorization."""
    if request_doc.get('owner')!=owner or max_seconds<=0 or type(max_submissions)!=int or max_submissions!=1 or not note.strip():
        raise ValueError('授权必须指定同一用户、正时长、仅一次提交和确认记录')
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('INSERT INTO generation_authorizations VALUES (?,?,?,?,?,?,?,?)',
                  (owner,request_key,generation_request_digest(request_doc),max_seconds,max_submissions,now()+3600,note,None))
        c.execute('INSERT INTO audit_events VALUES (?,?,?,?,?)',(uuid.uuid4().hex,owner,'authorize_bounded_generation',request_key,now()))

def generation_authorization(owner,request_key):
    with connect() as c:
        row=c.execute('SELECT * FROM generation_authorizations WHERE owner=? AND request_key=?',(owner,request_key)).fetchone()
    return dict(row) if row else None

def generation_availability(jobs=None):
    if jobs is None:
        with connect() as c:
            jobs=[json.loads(r['doc']) for r in c.execute('SELECT doc FROM jobs').fetchall()]
    blocked=sum(bool(j.get('submission_uncertain')) and not j.get('archived_at') for j in jobs)>=int(os.getenv('VIDEO_AGENT_MAX_UNCERTAIN_SUBMISSIONS','5'))
    return {'available':not blocked,'reason':GENERATION_PAUSED if blocked else None}

def archive_jobs(ids, reason):
    """Retire paused jobs from the active UI and global receipt limit; keep their evidence."""
    ids=list(dict.fromkeys(ids))
    if not ids or not reason.strip():raise ValueError('归档需要任务编号和原因')
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        rows=c.execute('SELECT id,doc FROM jobs WHERE id IN ('+','.join('?' for _ in ids)+')',ids).fetchall()
        if len(rows)!=len(ids):raise ValueError('部分任务编号不存在')
        jobs={r['id']:json.loads(r['doc']) for r in rows}
        for jid in ids:
            d=jobs[jid]
            if d.get('archived_at'):continue
            if d['status'] in RUNNING_STATES or (d.get('execution') or {}).get('expires',0)>now():
                raise ValueError('运行中的任务不能归档')
            if d.get('agent_submission_intent') or d.get('submission_intent') or any(
                b.get('state')=='pending' for b in d.get('agent_builds',{}).values()
            ):
                raise ValueError('仍有待确认的执行过程，不能归档')
        archived=[]
        for jid in ids:
            d=jobs[jid]
            if d.get('archived_at'):continue
            moment=now()
            d.update(archived_at=moment,archived_reason=reason,updated=moment)
            c.execute('UPDATE jobs SET updated=?,doc=? WHERE id=?',(moment,json.dumps(d,ensure_ascii=False),jid))
            c.execute('INSERT INTO audit_events VALUES (?,?,?,?,?)',
                      (uuid.uuid4().hex,d.get('owner','local'),'archive_job',jid,moment))
            archived.append(jid)
    return archived

def create(d,daily_limit=0,request_key=None,require_generation=False):
    def request_digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    # A server upgrade must not turn a browser's unchanged retry into new work.
    digest=generation_request_digest(d)
    compatible_digests={digest,request_digest(d),request_digest({k:v for k,v in d.items() if k!='source_version'})}
    d.update(id=d.get("id") or uuid.uuid4().hex,status="queued",created=now(),updated=now(),events=[],segments=d.get("segments",[]),error=None)
    d['timing_version']=1
    track_status(d,'queued')
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        if request_key:
            prior=c.execute('SELECT digest,job_id FROM request_keys WHERE owner=? AND request_key=?',(d.get('owner','local'),request_key)).fetchone()
            if prior:
                if prior['digest'] not in compatible_digests:raise ValueError('重复请求编号对应不同内容，请重新提交')
                return json.loads(c.execute('SELECT doc FROM jobs WHERE id=?',(prior['job_id'],)).fetchone()['doc'])
        approved=None
        if request_key:
            candidate=c.execute('SELECT * FROM generation_authorizations WHERE owner=? AND request_key=?',(d.get('owner','local'),request_key)).fetchone()
            if candidate:
                if candidate['digest']!=digest or candidate['job_id'] or candidate['expires']<=now():
                    raise ValueError('单次测试授权不匹配、已使用或已到期')
                approved=candidate
                d['generation_authorization_key']=request_key
        if require_generation and not approved:
            availability=generation_availability([json.loads(r['doc']) for r in c.execute('SELECT doc FROM jobs').fetchall()])
            if not availability['available']:raise ValueError(availability['reason'])
        if daily_limit:
            import datetime
            today=datetime.datetime.now().astimezone().replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
            rows=c.execute("SELECT doc FROM jobs WHERE updated>=?",(today,)).fetchall()
            used=sum(float(j.get("duration") or 0) for r in rows if (j:=json.loads(r['doc'])).get("owner","local")==d.get("owner","local") and j.get("created",0)>=today)
            if used+float(d["duration"] or 0)>daily_limit:raise ValueError("今日生成时长额度已用完")
        c.execute("INSERT INTO jobs VALUES (?,?,?,?)",(d["id"],d["status"],d["updated"],json.dumps(d,ensure_ascii=False)))
        if request_key:
            c.execute('INSERT INTO request_keys VALUES (?,?,?,?)',(d.get('owner','local'),request_key,digest,d['id']))
        if approved:
            c.execute('UPDATE generation_authorizations SET job_id=? WHERE owner=? AND request_key=?',(d['id'],d.get('owner','local'),request_key))
    return d
def get(i):
    with connect() as c:r=c.execute("SELECT doc FROM jobs WHERE id=?",(i,)).fetchone()
    if not r:raise KeyError("任务不存在")
    return json.loads(r['doc'])
def update(i, _expected_statuses=None, _absent=(), **values):
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        r=c.execute("SELECT doc FROM jobs WHERE id=?",(i,)).fetchone()
        if not r:raise KeyError(i)
        d=json.loads(r['doc'])
        if _expected_statuses is not None and d['status'] not in _expected_statuses:
            raise ValueError('任务状态已变化，请刷新后查看')
        execution.check(d, now())
        if any(d.get(key) for key in _absent):
            raise ValueError('远端提交尚未确认，不能重新启动')
        if 'status' in values:track_status(d,values['status'])
        d.update(values,updated=now())
        c.execute("UPDATE jobs SET status=?,updated=?,doc=? WHERE id=?",(d["status"],d["updated"],json.dumps(d,ensure_ascii=False),i))
    return d
def reserve_plan(i,plan,duration,daily_limit=0):
    """Commit an automatic duration and plan together before any paid generation."""
    if not 1<=duration<=180:raise ValueError("成片时长超出1至180秒范围")
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row=c.execute("SELECT doc FROM jobs WHERE id=?",(i,)).fetchone()
        if not row:raise KeyError(i)
        d=json.loads(row['doc'])
        execution.check(d, now())
        from .production_schema import valid_time
        if d.get("duration") is not None and not valid_time(d,duration):
            raise ValueError("方案时长与指定时长不一致")
        if daily_limit:
            import datetime
            today=datetime.datetime.now().astimezone().replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
            rows=c.execute("SELECT id,doc FROM jobs WHERE updated>=?",(today,)).fetchall()
            used=sum(float(j.get("duration") or 0) for r in rows if r["id"]!=i and (j:=json.loads(r["doc"])).get("owner","local")==d.get("owner","local") and j.get("created",0)>=today)
            if used+duration>daily_limit:raise ValueError("今日生成时长额度已用完")
        policy=d.get('timing_policy') or {'mode':'strict'}
        if policy['mode']=='approximate' and 'min' not in policy:
            policy.update(min=max(1,duration-1),max=min(180,duration+1))
        d.update(plan=plan,duration=duration,timing_policy=policy,updated=now())
        c.execute("UPDATE jobs SET status=?,updated=?,doc=? WHERE id=?",(d["status"],d["updated"],json.dumps(d,ensure_ascii=False),i))
    return d
def event(i,msg):
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT doc FROM jobs WHERE id=?',(i,)).fetchone()
        if not row:raise KeyError(i)
        d=json.loads(row['doc']);execution.check(d, now())
        d['events']=(d.get('events',[])+[{'time':now(),'message':msg}])[-150:]
        c.execute('UPDATE jobs SET doc=? WHERE id=?',(json.dumps(d,ensure_ascii=False),i))

def authorize_failed_generation(i,proof):
    from . import failed_generation,plan_budget,production
    from .production_schema import context_version
    import uuid
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        jobs=[json.loads(r['doc']) for r in c.execute('SELECT doc FROM jobs').fetchall()]
        d=next(j for j in jobs if j['id']==i);execution.check(d,now())
        if not any(x['build_id']==proof['build_id'] for x in failed_generation.candidates(d)):
            raise ValueError('任务状态已变化，请刷新后查看')
        build=next(b for b in d['agent_builds'].values() if b['build_id']==proof['build_id'])
        if proof.get('status') not in failed_generation.TERMINAL or proof.get('provider_task_id')!=build['provider_task_id'] or not 0<=now()-proof.get('checked_at',0)<120:
            raise ValueError('缺少近期供应商终态凭据')
        attempts=[b for b in d['agent_builds'].values() if b.get('unit_id')==build['unit_id'] and b.get('generation_seconds',0)]
        if len(attempts)>=production.unit_generation_limit(d):raise ValueError('该片段已达到生成次数上限，已有结果保留')
        approved=None
        if d.get('generation_authorization_key'):
            approved=c.execute('SELECT * FROM generation_authorizations WHERE owner=? AND request_key=?',
                (d.get('owner','local'),d['generation_authorization_key'])).fetchone()
            if not approved or approved['job_id']!=i:raise ValueError('单次测试授权与任务不匹配')
        d['plan_budget']=plan_budget.allocate(d,jobs,approval=approved)
        build['terminal_verified']=proof
        d.setdefault('failed_generation_retries',{})[build['build_id']]={
            'token':uuid.uuid4().hex,'context_version':context_version(d),'authorized_at':now(),
            'provider_proof':proof,'unit_id':build['unit_id']}
        track_status(d,'queued');d.update(status='queued',error=None,failure=None,next_run_at=0,updated=now())
        c.execute('UPDATE jobs SET status=?,updated=?,doc=? WHERE id=?',('queued',d['updated'],json.dumps(d,ensure_ascii=False),i))
    return d

def ensure_plan_budget(i):
    """Reserve the entire remaining first pass atomically across this owner's jobs."""
    from . import plan_budget
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        jobs=[json.loads(r['doc']) for r in c.execute('SELECT doc FROM jobs').fetchall()]
        d=next(j for j in jobs if j['id']==i);execution.check(d,now())
        approved=None
        if d.get('generation_authorization_key'):
            approved=c.execute('SELECT * FROM generation_authorizations WHERE owner=? AND request_key=?',
                (d.get('owner','local'),d['generation_authorization_key'])).fetchone()
            if not approved or approved['job_id']!=i:raise ValueError('单次测试授权与任务不匹配')
        d['plan_budget']=plan_budget.allocate(d,jobs,approval=approved)
        c.execute('UPDATE jobs SET doc=? WHERE id=?',(json.dumps(d,ensure_ascii=False),i))
    return d['plan_budget']

def reserve_submission(i,intent,seconds,budget):
    """Reserve provider capacity and the job budget in the same transaction."""
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        jobs=[json.loads(r['doc']) for r in c.execute('SELECT doc FROM jobs').fetchall()]
        d=next(j for j in jobs if j['id']==i)
        execution.check(d, now())
        if d.get('agent_submission_intent') or d.get('submission_uncertain'):
            raise ValueError('上次提交回执不确定，需核对，不重复付费')
        used=d.get('reserved_generation_seconds',0)
        approved=None
        if d.get('generation_authorization_key'):
            approved=c.execute('SELECT * FROM generation_authorizations WHERE owner=? AND request_key=?',
                (d.get('owner','local'),d['generation_authorization_key'])).fetchone()
            if not approved or approved['job_id']!=i:raise ValueError('单次测试授权与任务不匹配')
            if seconds and (approved['expires']<=now() or used+seconds>approved['max_seconds'] or d.get('authorized_submission_count',0)>=approved['max_submissions']):
                raise ValueError('已达到用户授权的单次测试次数、时长或有效期上限')
        if seconds and used+seconds>budget:raise ValueError('生成预算不足')
        if seconds and d.get('workflow_version',0)>=4:
            from . import plan_budget
            budget_hold=plan_budget.allocate(d,jobs,seconds=seconds,
                unit_id=intent.get('record',{}).get('unit_id'),limit=budget,approval=approved)
        owner=d.get('owner','local')
        daily_limit=int(os.getenv('VIDEO_AGENT_DAILY_GENERATION_SECONDS','0' if owner=='local' else '600'))
        import datetime
        date=datetime.datetime.now().astimezone()
        day=date.date().isoformat()
        today=date.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        def daily_reserved(j):
            ledger=j.get('generation_daily_reservations')
            return ledger.get(day,0) if ledger is not None else j.get('reserved_generation_seconds',0) if j.get('created',0)>=today else 0
        if seconds and daily_limit:
            spent=sum(daily_reserved(j) for j in jobs if j.get('owner','local')==owner)
            if d.get('workflow_version',0)<4:
                spent+=sum(j.get('plan_budget',{}).get('remaining_seconds',0) for j in jobs
                    if j['id']!=i and j.get('owner','local')==owner and not j.get('archived_at')
                    and j.get('status')!='completed' and j.get('plan_budget',{}).get('day')==day
                    and j.get('plan_budget',{}).get('expires',0)>now())
            if spent+seconds>daily_limit:raise ValueError('今日预留生成预算不足（包含返修和待核对提交）')
        if seconds and not approved and not generation_availability(jobs)['available']:
            raise ValueError(GENERATION_PAUSED)
        active=sum(1 for j in jobs for b in j.get('agent_builds',{}).values() if b.get('state')=='pending' and b.get('generation_seconds',0)>0)
        active+=sum(bool(j.get('agent_submission_intent',{}).get('record',{}).get('generation_seconds')) for j in jobs if j.get('agent_submission_intent'))
        # An unresolved receipt blocks another submission for that same job.
        # It is not proof of an in-flight provider build and must not reserve
        # a global slot indefinitely after the local build has failed.
        if seconds and active>=int(os.getenv('VIDEO_AGENT_REMOTE_CONCURRENT','2')):
            d['capacity_wait']=True
            c.execute('UPDATE jobs SET doc=? WHERE id=?',(json.dumps(d,ensure_ascii=False),i))
            return False
        if seconds and d.get('workflow_version',0)>=4:
            from . import failed_generation
            unit_id=intent.get('record',{}).get('unit_id')
            failed_generation.require_authorization(d,unit_id)
            prior=failed_generation.latest(d,unit_id)
            if prior and prior.get('state')=='failed':
                grant=failed_generation.authorization(d,prior)
                if intent.get('record',{}).get('retry_token')!=grant['token']:
                    raise ValueError('新版本未绑定失败重试授权')
                grant['consumed_at']=now()
            d['plan_budget']=budget_hold
        today_reserved=daily_reserved(d)
        ledger=d.setdefault('generation_daily_reservations',{})
        ledger[day]=today_reserved+seconds
        if seconds and approved:d['authorized_submission_count']=d.get('authorized_submission_count',0)+1
        d.update(agent_submission_intent=intent,reserved_generation_seconds=used+seconds,capacity_wait=False)
        c.execute('UPDATE jobs SET doc=? WHERE id=?',(json.dumps(d,ensure_ascii=False),i))
        return True
def record_tool_timing(i, action, seconds, failed=False):
    """Small atomic timing counters; no prompts, inputs or credentials."""
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT doc FROM jobs WHERE id=?',(i,)).fetchone()
        if not row:return
        d=json.loads(row['doc']);execution.check(d, now());timings=d.setdefault('tool_timings',{})
        entry=timings.setdefault(action,{'calls':0,'failed':0,'seconds':0})
        entry['calls']+=1;entry['failed']+=int(failed)
        entry['seconds']=round(entry['seconds']+seconds,3)
        # Telemetry alone does not change queue ordering or the visible task state.
        c.execute('UPDATE jobs SET doc=? WHERE id=?',(json.dumps(d,ensure_ascii=False),i))
def list_jobs(owner=None,limit=100,include_archived=False):
    # Filter ownership before limiting; another user's recent jobs must not hide yours.
    with connect() as c:rows=c.execute("SELECT doc FROM jobs ORDER BY updated DESC").fetchall()
    return [d for r in rows if (d:=json.loads(r['doc'])) and
            (include_archived or not d.get('archived_at')) and
            (owner is None or d.get('owner','local')==owner)][:limit]
def daily_seconds(owner):
    import datetime
    today=datetime.datetime.now().astimezone().replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    with connect() as c:rows=c.execute("SELECT doc FROM jobs WHERE updated>=?",(today,)).fetchall()
    return sum(float(j.get("duration") or 0) for r in rows if (j:=json.loads(r['doc'])).get("owner","local")==owner and j.get("created",0)>=today)
def recover_queue():
    # Hypit/remote jobs may still run. Keep their IDs; resume by querying them.
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        rows=c.execute('SELECT doc FROM jobs').fetchall()
        for row in rows:
            d=json.loads(row['doc'])
            if d['status'] not in RUNNING_STATES or (d.get('execution') or {}).get('expires',0)>now():continue
            # Persisted IDs/intents survive recovery. A live owner is never reset.
            track_status(d,'queued')
            d.update(status='queued',execution=None,updated=now())
            c.execute('UPDATE jobs SET status=?,updated=?,doc=? WHERE id=?',('queued',d['updated'],json.dumps(d,ensure_ascii=False),d['id']))
    # Correct historical status labels without deleting any finished media.
    from shutil import copy2
    with connect() as c:
        completed=c.execute("SELECT doc FROM jobs WHERE status='completed'").fetchall()
    for row in completed:
        job=json.loads(row['doc'])
        if job.get('engine')=='hypit-agent-v5':continue
        reviews=list((job.get('reviews') or {}).values())
        failed=any(r.get('verdict')=='fail' for r in reviews)
        if failed or not reviews or any(r.get('verdict')!='pass' for r in reviews):
            root=DATA/'jobs'/job['id']
            if (root/'final.mp4').is_file() and not (root/'candidate.mp4').exists():copy2(root/'final.mp4',root/'candidate.mp4')
            update(job['id'],status='needs_revision' if failed else 'needs_review',error='历史任务的检查未通过或不完整，已修正完成状态，原视频保留供预览。')
def claim(exclude=()):
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        rows=c.execute("SELECT doc FROM jobs ORDER BY updated").fetchall()
        jobs=[json.loads(r['doc']) for r in rows]
        moment=now()
        active=[j for j in jobs if (j.get('execution') or {}).get('expires',0)>moment]
        remote=sum(1 for j in jobs for b in j.get('agent_builds',{}).values() if b.get('state')=='pending' and b.get('generation_seconds',0)>0)
        cap=int(os.getenv('VIDEO_AGENT_REMOTE_CONCURRENT','2'))
        per_user=int(os.getenv('VIDEO_AGENT_USER_CONCURRENT','2'))
        # Due remote queries first; new work waits while all remote slots are occupied.
        candidates=[j for j in jobs if j['id'] not in exclude and
                    (j.get('execution') or {}).get('expires',0)<=moment and
                    (j['status'] in ('queued','waiting_build') or
                     j['status'] in RUNNING_STATES and j.get('execution')) and
                    j.get('next_run_at',0)<=moment]
        last_served={}
        for j in jobs:
            owner=j.get('owner','local')
            last_served[owner]=max(last_served.get(owner,0),j.get('last_claim_at',0))
        candidates.sort(key=lambda j:(j['status']!='waiting_build',last_served.get(j.get('owner','local'),0),j['updated']))
        d=None
        for candidate in candidates:
            owned={j['id']:j for j in active+([j for j in jobs if j['status']=='waiting_build'] if candidate['status']=='queued' else [])}
            if sum(j['id']!=candidate['id'] and j.get('owner','local')==candidate.get('owner','local') for j in owned.values())>=per_user:continue
            if candidate['status']=='queued' and remote>=cap and not candidate.get('agent_builds'):continue
            d=candidate;break
        if d is None:return None
        track_status(d,'planning')
        d['status']='planning';d['updated']=now()
        d['execution_epoch']=d.get('execution_epoch',0)+1
        d['last_claim_at']=moment
        d['execution']={'token':uuid.uuid4().hex,'epoch':d['execution_epoch'],
                        'started':moment,'expires':moment+LEASE_SECONDS}
        c.execute("UPDATE jobs SET status=?,updated=?,doc=? WHERE id=?",("planning",d["updated"],json.dumps(d,ensure_ascii=False),d['id']))
    return d

def renew(i, token):
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        d=json.loads(c.execute('SELECT doc FROM jobs WHERE id=?',(i,)).fetchone()['doc'])
        lease=d.get('execution') or {}
        if lease.get('token')!=token or lease.get('expires',0)<=now():
            raise execution.ExecutionLost('任务执行权已失效')
        lease['expires']=now()+LEASE_SECONDS
        c.execute('UPDATE jobs SET doc=? WHERE id=?',(json.dumps(d,ensure_ascii=False),i))

def release(i, token):
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        d=json.loads(c.execute('SELECT doc FROM jobs WHERE id=?',(i,)).fetchone()['doc'])
        if (d.get('execution') or {}).get('token')!=token:return False
        d['execution']=None
        c.execute('UPDATE jobs SET doc=? WHERE id=?',(json.dumps(d,ensure_ascii=False),i))
        return True

def assert_execution(i):
    execution.check(get(i),now())

def claim_local(i, states):
    """Serialize a user-requested local operation against retries and the Worker."""
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        d=json.loads(c.execute('SELECT doc FROM jobs WHERE id=?',(i,)).fetchone()['doc'])
        execution.check(d,now())
        if d['status'] not in states:raise ValueError('当前制作仍在运行，请稍后重试')
        d['execution_epoch']=d.get('execution_epoch',0)+1
        d['execution']={'token':uuid.uuid4().hex,'epoch':d['execution_epoch'],'started':now(),'expires':now()+LEASE_SECONDS}
        c.execute('UPDATE jobs SET doc=? WHERE id=?',(json.dumps(d,ensure_ascii=False),i))
        return d
