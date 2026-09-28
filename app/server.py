"""Local staff-facing API. Bind to loopback for the initial pilot."""
import fcntl
import asyncio
import json
import os
import tempfile
import uuid
from shutil import copy2, copytree, ignore_patterns
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator

from . import config, media, store, worker, planner, auth, hypit_adapter, intent
from . import runtime

LOCK = None


@asynccontextmanager
async def lifespan(app):
    global LOCK
    config.DATA.mkdir(parents=True, exist_ok=True)
    runtime.validate()
    if auth.users():auth.secret()
    if not runtime.embedded_worker():
        with store.connect() as c:c.execute('SELECT 1')
        yield
        return
    with runtime.QueueLeader():
        thread=worker.start()
        try:yield
        finally:
            worker.STOP.set()
            await asyncio.to_thread(thread.join)


app = FastAPI(title="Hypit 视频助手", lifespan=lifespan)


@app.middleware("http")
async def login_guard(request:Request,call_next):
    if runtime.production() and request.method not in ('GET','HEAD','OPTIONS'):
        origin=request.headers.get('origin')
        from urllib.parse import urlsplit
        expected=urlsplit(os.getenv('VIDEO_AGENT_PUBLIC_URL',''))
        if origin and origin.rstrip('/')!=f'{expected.scheme}://{expected.netloc}':
            return JSONResponse({'detail':'请求来源不允许'},status_code=403)
    if request.url.path.startswith("/api/") and request.url.path not in ("/api/login","/api/health","/api/ready"):
        try:request.state.user=auth.actor(request)
        except HTTPException as exc:return JSONResponse({"detail":exc.detail},status_code=exc.status_code)
    return await call_next(request)


class NewJob(BaseModel):
    prompt: str = Field(default="",max_length=3000)
    duration: float | None = Field(default=None,ge=1,le=180)
    ratio: str | None = None
    images: list[str] = Field(default_factory=list,max_length=6)
    reference: str | None = None
    timing_mode: Literal['strict','approximate'] = 'strict'
    max_unit_generations: int | None = Field(default=None,ge=1,le=3,strict=True)

    @model_validator(mode="after")
    def nonempty(self):
        self.prompt = self.prompt.strip()
        if not (self.prompt or self.images or self.reference):
            raise ValueError("请至少提供文字、图片或参考视频中的一项")
        return self


class Revision(BaseModel):
    index: int = Field(ge=0)
    instruction: str = Field(min_length=3,max_length=1000)


class Credentials(BaseModel):
    username: str = Field(max_length=100)
    token: str = Field(max_length=512)


@app.post("/api/login")
def login(data:Credentials,response:Response,request:Request):
    if not auth.users():
        if auth.required():raise HTTPException(401,'当前没有可登录账号')
        return {"user":"local"}
    auth.login_attempt('ip:'+(request.client.host if request.client else 'unknown'),limit=60)
    auth.login_attempt('user:'+data.username)
    session=auth.login(data.username,data.token)
    response.set_cookie("hypit_session",session,httponly=True,samesite="strict",secure=os.getenv("VIDEO_AGENT_COOKIE_SECURE")=="1",max_age=24*3600)
    return {"user":data.username}

@app.post('/api/logout')
def logout(response:Response,request:Request):
    auth.revoke(request.cookies.get('hypit_session',''))
    response.delete_cookie('hypit_session')
    return {'ok':True}

@app.get('/api/me')
def me(request:Request):
    try:auth.require_admin(request.state.user);admin=True
    except HTTPException:admin=False
    return {'user':request.state.user,'admin':admin,'authentication':auth.required()}

@app.get('/api/admin/status')
def admin_status(request:Request):
    auth.require_admin(request.state.user)
    jobs=store.list_jobs(limit=None)
    return {'worker_alive':runtime.worker_alive(),'reference_configured':bool(config.media_base_url()),
            'counts':{s:sum(j['status']==s for j in jobs) for s in {j['status'] for j in jobs}},
            'attention':[{'id':j['id'],'owner':j.get('owner','local'),'status':j['status'],'error':j.get('error'),
                          'failure':j.get('failure')} for j in jobs if j['status'] in ('failed','needs_attention','needs_configuration','needs_review')]}

@app.get('/api/ready')
def ready():
    try:
        with store.connect() as c:c.execute('SELECT 1')
        available=runtime.embedded_worker() or runtime.worker_alive()
        return JSONResponse({'ready':available},status_code=200 if available else 503)
    except Exception:return JSONResponse({'ready':False},status_code=503)


def public(job):
    result={k:v for k,v in job.items() if k not in ("execution","video","agent_builds","agent_submission_intent","generation_manifests","reference_analysis","reference_partials","subject_spec","provider_profile","unit_reviews","archived_builds")}
    from .candidate import available
    result['saved_clips']=available(job)
    root=config.DATA/'jobs'/job['id']
    result['candidate_available']=(root/'candidate.mp4').is_file()
    shown=root/('final.mp4' if job['status']=='completed' else 'candidate.mp4')
    result['result_version']=f'{shown.stat().st_mtime_ns}-{shown.stat().st_size}' if shown.is_file() else None
    result['needs_reconciliation']=bool(job.get('agent_submission_intent') or job.get('submission_intent') or job.get('submission_uncertain'))
    seconds=dict(job.get('status_seconds',{}));clock=job.get('stage_clock')
    if clock and clock['status'] in ('queued','planning','analyzing','preparing_assets','generating','checking','rendering','agent_running','waiting_build'):
        seconds[clock['status']]=round(seconds.get(clock['status'],0)+max(0,store.now()-clock['started']),1)
    result['stage_seconds']=seconds
    result['timing_complete']=job.get('timing_version')==1
    if job['status']=='waiting_build':
        pending=[b for b in job.get('agent_builds',{}).values() if b.get('state')=='pending']
        result['status_label']='正在合成视频' if pending and all(b.get('generation_seconds')==0 for b in pending) else '等待视频生成'
    return result

def transition(job_id,states,**values):
    try:return store.update(job_id,_expected_statuses=states,**values)
    except ValueError as exc:raise HTTPException(409,str(exc))


@app.get("/")
def index():
    return FileResponse(config.ROOT/"static"/"index.html",media_type="text/html",headers={'Cache-Control':'no-cache'})


@app.get("/app.js")
def script():
    return FileResponse(config.ROOT/"static"/"app.js",media_type="text/javascript",headers={'Cache-Control':'no-cache'})


@app.get("/style.css")
def style():
    return FileResponse(config.ROOT/"static"/"style.css",media_type="text/css")


@app.get("/api/health")
def health():
    return {"ok":True,"hypit":config.HYPIT.is_file(),"ffmpeg":Path(config.FFMPEG).is_file(),
            "agent_runtime":config.DSH.is_file(),"reference_ready":bool(config.media_base_url()),
            'generation':store.generation_availability(),'features':{'revisions':config.ENABLE_REVISIONS}}


@app.post("/api/assets")
async def upload(request:Request,kind:str=Query(pattern="^(image|video)$"),name:str="upload"):
    total=0
    config.DATA.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=config.DATA,delete=False) as tmp:
        path=Path(tmp.name)
        try:
            async for chunk in request.stream():
                total+=len(chunk)
                if total>config.MAX_UPLOAD:
                    raise HTTPException(413,"文件超过150MB")
                tmp.write(chunk)
        except:
            path.unlink(missing_ok=True)
            raise
    if not total:
        path.unlink(missing_ok=True)
        raise HTTPException(400,"空文件")
    try:
        result=media.save_upload(path,kind,name,request.state.user)
        return {k:v for k,v in result.items() if k!="path"}
    except Exception as exc:
        raise HTTPException(400,str(exc)[:500])
    finally:
        path.unlink(missing_ok=True)


@app.post("/api/jobs")
def create(data:NewJob,request:Request):
    if data.ratio is not None and data.ratio not in intent.RATIOS:
        raise HTTPException(400,"画幅须为自动、16:9、9:16或1:1")
    if len(data.images)!=len(set(data.images)):
        raise HTTPException(400,"图片不能重复")
    limit=int(os.getenv("VIDEO_AGENT_DAILY_SECONDS","600" if auth.users() else "0"))
    key=request.headers.get('Idempotency-Key')
    if key and (len(key)>128 or not all(ch.isalnum() or ch in '-_' for ch in key)):
        raise HTTPException(400,'无效的重复提交保护编号')
    for aid in data.images:
        try: a=store.asset(aid)
        except KeyError: raise HTTPException(400,"图片不存在")
        auth.require_owned(a,request.state.user)
        if a["kind"]!="image":raise HTTPException(400,"图片类型错误")
    if data.reference:
        try: ref=store.asset(data.reference)
        except KeyError: raise HTTPException(400,"参考视频不存在")
        auth.require_owned(ref,request.state.user)
        if ref["kind"]!="video":raise HTTPException(400,"参考素材不是视频")
    try:
        supplied=data.model_dump()
        if supplied['max_unit_generations'] is None:
            supplied.pop('max_unit_generations')
        resolved=intent.resolve(supplied)
        return public(store.create({**resolved,"owner":request.state.user,"engine":"hypit-agent-v5","production_version":2,"workflow_version":3,"source_version":config.SOURCE_VERSION},daily_limit=limit,request_key=key,require_generation=True))
    except ValueError as exc:raise HTTPException(503 if str(exc)==store.GENERATION_PAUSED else 409 if '重复请求' in str(exc) else 429,str(exc))


@app.get("/api/jobs")
def jobs(request:Request):
    return [public(j) for j in store.list_jobs(owner=request.state.user)]


@app.get("/api/jobs/{job_id}")
def job(job_id:str,request:Request):
    try:doc=store.get(job_id)
    except KeyError:raise HTTPException(404,"任务不存在")
    auth.require_owned(doc,request.state.user)
    return public(doc)


@app.get("/api/jobs/{job_id}/video")
def video(job_id:str,request:Request,candidate:bool=False):
    try:doc=store.get(job_id)
    except KeyError:raise HTTPException(404,"任务不存在")
    auth.require_owned(doc,request.state.user)
    path=config.DATA/"jobs"/job_id/("candidate.mp4" if candidate else "final.mp4")
    if (not candidate and doc["status"]!="completed") or not path.is_file():raise HTTPException(404,"视频尚未完成")
    return FileResponse(path,media_type="video/mp4",filename="hypit-video.mp4")


@app.get("/api/jobs/{job_id}/clips/{index}")
def clip(job_id:str,index:int,request:Request):
    try:doc=store.get(job_id)
    except KeyError:raise HTTPException(404,"任务不存在")
    auth.require_owned(doc,request.state.user)
    if index<0 or index>=len((doc.get("plan") or {}).get("segments",[])):
        raise HTTPException(404,"片段不存在")
    path=config.DATA/"jobs"/job_id/"assets"/f"clip{index}.mp4"
    if doc.get('engine')=='hypit-agent-v5':
        # Narrative segments can share one generated take. Preview their actual timeline range.
        root=config.DATA/'jobs'/job_id
        source=root/('final.mp4' if doc['status']=='completed' else 'candidate.mp4')
        if not source.is_file():raise HTTPException(404,'片段预览将在完整视频输出后提供')
        import hashlib
        segments=doc['plan']['segments'];start=sum(float(s['duration']) for s in segments[:index]);duration=float(segments[index]['duration'])
        key=hashlib.sha256(f'{source.stat().st_mtime_ns}:{start}:{duration}'.encode()).hexdigest()[:20]
        path=root/'previews'/f'{index}-{key}.mp4'
        if not path.exists():
            path.parent.mkdir(exist_ok=True)
            media.command([config.FFMPEG,'-v','error','-y','-ss',str(start),'-i',str(source),'-t',str(duration),'-c:v','libx264','-c:a','aac',str(path)],timeout=180)
    if not path.is_file():raise HTTPException(404,"片段尚未完成")
    return FileResponse(path,media_type="video/mp4")


@app.get('/api/jobs/{job_id}/versions/{build_id}')
def generated_version(job_id:str,build_id:str,request:Request,download:bool=False):
    try:doc=store.get(job_id)
    except KeyError:raise HTTPException(404,'任务不存在')
    auth.require_owned(doc,request.state.user)
    from .candidate import available
    version=next((v for v in available(doc) if v['build_id']==build_id),None)
    if not version:raise HTTPException(404,'该生成版本尚未保存或文件已变化')
    return FileResponse(config.DATA/'jobs'/job_id/'project'/version['path'],
                        media_type='video/mp4',filename='hypit-clip.mp4' if download else None)


@app.post("/api/jobs/{job_id}/retry")
def retry(job_id:str,request:Request):
    try:doc=store.get(job_id)
    except KeyError:raise HTTPException(404,"任务不存在")
    auth.require_owned(doc,request.state.user)
    if doc["status"] not in ('failed','needs_configuration','needs_attention','needs_review','needs_revision'):raise HTTPException(409,"当前任务不能重复启动")
    if doc.get("submission_intent") or doc.get('agent_submission_intent') or doc.get('submission_uncertain'):raise HTTPException(409,"远端提交状态不确定，不能直接重试；已有片段仍可合成")
    if doc.get('engine')!='hypit-agent-v5' and doc['status'] in ['needs_review','needs_revision']:
        raise HTTPException(409,'旧任务请使用修改入口创建新版任务')
    transition(job_id,('failed','needs_configuration','needs_attention','needs_review','needs_revision'),
               _absent=('submission_intent','agent_submission_intent','submission_uncertain'),
               status="queued",error=None,failure=None,retry_requested=True,transport_retries=0,next_run_at=0)
    store.event(job_id,"已继续处理，保留现有片段和任务编号")
    return public(store.get(job_id))


@app.post("/api/jobs/{job_id}/reconcile")
def reconcile(job_id:str,request:Request):
    try:doc=store.get(job_id)
    except KeyError:raise HTTPException(404,"任务不存在")
    auth.require_owned(doc,request.state.user)
    if doc.get('submission_uncertain'):
        return {'reconciled':False,'message':'服务商提交未返回可靠任务编号，本地记录无法证明未受理。请管理员核对服务商记录；已有片段可直接合成。'}
    if doc['status']=='needs_attention' and doc.get('agent_submission_intent'):
        from datetime import datetime
        pending=doc['agent_submission_intent'];root=config.DATA/'jobs'/job_id/'project'
        found=[b for b in hypit_adapter.builds(root) if b.get('run')==pending['run'] and datetime.fromisoformat(b['createdAt'].replace('Z','+00:00')).timestamp()>=pending.get('created',0)-2]
        if len(found)!=1:
            return {'reconciled':False,'message':'未找到唯一对应的 Hypit 提交回执；保留工程，请管理员核对本地提交日志与服务商任务，暂不重复付费。'}
        builds=doc.get('agent_builds',{});builds[pending['stamp']]={**builds.get(pending['stamp'],{}),**pending.get('record',{}),'build_id':found[0]['id'],'run':pending['run'],'output':pending['output'],'state':'pending'}
        transition(job_id,('needs_attention',),agent_builds=builds,agent_submission_intent=None,status='queued',error=None)
        store.event(job_id,'已找回原提交回执，继续查询原任务')
        return {'reconciled':True,'job':public(store.get(job_id))}
    if doc["status"]!="needs_attention" or doc.get("submission_intent") is None:
        raise HTTPException(409,"当前任务无需核对未确认的提交")
    intent=doc["submission_intent"]
    run_name="film.svrun" if intent=="render" else f"segment-{intent}.svrun"
    root=config.DATA/"jobs"/job_id
    found=[b for b in hypit_adapter.builds(root) if b.get("run")==run_name]
    if not found:
        return {"reconciled":False,"message":"Hypit 未找到对应 Build；请由管理员核对服务商任务和本地提交日志，暂不重新付费提交。"}
    found.sort(key=lambda b:b.get("createdAt",""),reverse=True)
    bid=found[0]["id"]
    if intent=="render":
        transition(job_id,('needs_attention',),render_build=bid,submission_intent=None,status="queued",error=None)
    else:
        builds=doc.get("segment_builds",{})
        builds[intent]=bid
        transition(job_id,('needs_attention',),segment_builds=builds,submission_intent=None,status="queued",error=None)
    store.event(job_id,f"已找回 Hypit Build {bid}，继续查询原任务")
    return {"reconciled":True,"job":public(store.get(job_id))}


@app.post("/api/jobs/{job_id}/revisions")
def revise(job_id:str,change:Revision,request:Request):
    if not config.ENABLE_REVISIONS:raise HTTPException(409,'指定片段修改暂未开放')
    try:doc=store.get(job_id)
    except KeyError:raise HTTPException(404,"任务不存在")
    auth.require_owned(doc,request.state.user)
    if doc["status"] not in ('completed','needs_review','needs_revision','failed','needs_attention'):raise HTTPException(409,"请等待当前制作结束后修改")
    if doc.get('agent_submission_intent') or doc.get('submission_intent'):raise HTTPException(409,'请先核对未确认的付费请求')
    limit=int(os.getenv("VIDEO_AGENT_DAILY_SECONDS","600" if auth.users() else "0"))
    if limit and store.daily_seconds(request.state.user)+doc["duration"]>limit:
        raise HTTPException(429,"今日生成时长额度已用完")
    old=doc.get("plan") or {"segments":[]}
    if change.index>=len(old["segments"]):raise HTTPException(400,"片段序号不存在")
    # Revisions use the same production tools, with already-generated assets available for reuse.
    fresh={k:doc.get(k) for k in ('prompt','duration','ratio','images','reference','owner','duration_mode','inferred_intent','effective_prompt','reference_mode')}
    fresh.update(id=uuid.uuid4().hex,engine='hypit-agent-v5',parent_id=job_id,
                 feedback=f'修改第 {change.index+1} 段：{change.instruction}。尽量复用其他已生成内容，检查前后衔接。',plan=old)
    source=config.DATA/'jobs'/job_id
    previous=source/'project' if (source/'project').is_dir() else source
    target=config.DATA/'jobs'/fresh['id']/'project'
    copytree(previous,target,ignore=ignore_patterns('node_modules','.hypit','harness','agent-*','submission-journal','final.mp4','candidate.mp4'))
    try:return public(store.create(fresh,daily_limit=limit))
    except ValueError as exc:raise HTTPException(429,str(exc))


class Answer(BaseModel):
    instruction: str = Field(min_length=1,max_length=3000)


class CandidateSelection(BaseModel):
    build_ids: list[str] = Field(min_length=1,max_length=50)


@app.post('/api/jobs/{job_id}/compose-candidate')
def compose_candidate(job_id:str,data:CandidateSelection,request:Request):
    from . import candidate
    try:doc=store.get(job_id)
    except KeyError:raise HTTPException(404,'任务不存在')
    auth.require_owned(doc,request.state.user)
    try:candidate.compose(doc,data.build_ids)
    except ValueError as exc:raise HTTPException(409,str(exc))
    return public(store.get(job_id))

@app.post("/api/jobs/{job_id}/answer")
def answer(job_id:str,data:Answer,request:Request):
    try:doc=store.get(job_id)
    except KeyError:raise HTTPException(404,"任务不存在")
    auth.require_owned(doc,request.state.user)
    if doc["status"]!="needs_input":raise HTTPException(409,"当前任务没有待回答的问题")
    return public(transition(job_id,('needs_input',),feedback=(doc.get("feedback") or "")+"\n用户补充："+data.instruction,question=None,status="queued",error=None,next_run_at=0,transport_retries=0))
