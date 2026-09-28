"""One queue owner, bounded concurrent jobs, one execution per claimed job."""
import json
import os
import threading
import time
import traceback
from pathlib import Path
from shutil import copy2
from concurrent.futures import ThreadPoolExecutor

from . import analyzer, compiler, config, hypit_adapter as hypit, media, planner, store, image_asset, execution

STOP = threading.Event()


def phase(job_id, status, message):
    store.update(job_id,status=status,error=None)
    store.event(job_id,message)


def wait_build(job_id, root, profile, build_id, label):
    deadline=time.monotonic()+35*60
    while time.monotonic()<deadline:
        view=hypit.status(root,build_id,profile)
        build=view.get("build") or {}
        state=(build.get("work") or {}).get("state")
        outcome=(build.get("work") or {}).get("outcome") or (build.get("result") or {}).get("state")
        if outcome=="complete" or state=="done" and outcome=="complete":
            return
        if outcome in ("failed","cancelled","interrupted") or state=="done":
            raise RuntimeError(f"{label}失败："+str(build.get("failure") or outcome))
        if STOP.wait(15): return
    raise RuntimeError(f"{label}等待超时；任务编号 {build_id} 已保存，请核对远端任务")


def process(job):
    if job.get('workflow_version') in (3,4):
        from .pipeline import run
        return run(job,STOP)
    if job.get('engine')=='hypit-agent-v5':
        from . import agent_runner
        return agent_runner.process(job,STOP)
    jid=job["id"]
    root=config.DATA/"jobs"/jid
    root.mkdir(parents=True,exist_ok=True)
    try:
        job=store.get(jid)
        if not job.get("analysis"):
            phase(jid,"analyzing","正在理解文字和素材")
            analysis=analyzer.analyze(job,root)
            store.update(jid,analysis=analysis)
            (root/"analysis.json").write_text(json.dumps(analysis,ensure_ascii=False,indent=2))
        job=store.get(jid)
        if not job.get("plan"):
            phase(jid,"planning","正在创作视频方案")
            plan=planner.plan(job,root,job["analysis"])
            for seg in plan["segments"]:
                ids=seg.get("image_ids",[])
                if len(ids)!=len(set(ids)) or any(a not in job["images"] for a in ids):
                    raise ValueError("方案引用了不存在或重复的图片")
            duration=sum(s["duration"] for s in plan["segments"])
            limit=int(os.getenv("VIDEO_AGENT_DAILY_SECONDS","600" if job.get("owner","local")!="local" else "0"))
            store.reserve_plan(jid,plan,duration,daily_limit=limit)
            (root/"plan.json").write_text(json.dumps(plan,ensure_ascii=False,indent=2))
        job=store.get(jid)
        plan=job["plan"]
        compiler.prepare(job,plan,root)
        profile=hypit.setup(root)
        if any(s.get("use_shared_subject") for s in plan["segments"]):
            if not plan.get("shared_subject_prompt"):
                raise ValueError("方案需要统一主体图片，但缺少生图描述")
            phase(jid,"preparing_assets","正在生成统一主体参考图")
            image_asset.create_shared_subject(root,plan["shared_subject_prompt"],job["ratio"])
        for i,segment in enumerate(plan["segments"]):
            clip=root/"assets"/f"clip{i}.mp4"
            if clip.exists():
                media.verify(clip,segment["duration"])
                continue
            phase(jid,"generating",f"正在生成第 {i+1}/{len(plan['segments'])} 段")
            run=compiler.segment_source(job,segment,i,root)
            hypit.check(root,run)
            hypit.plan(root,run,profile)
            job=store.get(jid)
            builds=job.get("segment_builds",{})
            key=str(i)
            if key not in builds:
                if job.get("submission_intent")==key:
                    raise RuntimeError("片段提交状态不确定，已暂停，避免重复付费")
                store.update(jid,submission_intent=key)
                accepted=hypit.build(root,run,profile)
                build_id=(accepted.get("build") or {}).get("id")
                if not build_id:raise RuntimeError("Hypit 提交回执没有 Build ID，已暂停")
                builds[key]=build_id
                store.update(jid,segment_builds=builds,submission_intent=None)
            build_id=builds[key]
            wait_build(jid,root,profile,build_id,f"第{i+1}段")
            hypit.get(root,build_id,"clip.video",clip)
            media.verify(clip,segment["duration"])
            store.event(jid,f"第 {i+1} 段已保存")
        phase(jid,"checking","正在抽样检查片段画面")
        reviews=store.get(jid).get("reviews",{})
        for i,segment in enumerate(plan["segments"]):
            if str(i) in reviews:continue
            try:
                reviews[str(i)]=planner.review(job,root/"assets"/f"clip{i}.mp4",segment,root/f"review-{i}")
            except Exception as exc:
                reviews[str(i)]={"verdict":"warn","issues":["抽样视觉检查暂不可用"],"summary":str(exc)[:180]}
            store.update(jid,reviews=reviews)
        if any(r.get('verdict')=='fail' for r in reviews.values()):
            store.update(jid,status='needs_revision',error='片段检查未通过，请修改后重新生成。')
            return
        if any(r.get('verdict')!='pass' for r in reviews.values()):
            store.update(jid,status='needs_review',error='部分内容尚未通过检查，需复核。')
            return
        phase(jid,"rendering","正在合成完整视频")
        target=root/"final.mp4"
        if not target.exists():
            run=compiler.film_source(job,plan,root,[s["duration"] for s in plan["segments"]])
            hypit.check(root,run)
            hypit.plan(root,run,profile)
            job=store.get(jid)
            build_id=job.get("render_build")
            if not build_id:
                if job.get("submission_intent")=="render":
                    raise RuntimeError("合成提交状态不确定，请核对任务")
                store.update(jid,submission_intent="render")
                accepted=hypit.build(root,run,profile)
                build_id=(accepted.get("build") or {}).get("id")
                if not build_id:raise RuntimeError("Hypit 合成回执无 Build ID")
                store.update(jid,render_build=build_id,submission_intent=None)
            wait_build(jid,root,profile,build_id,"合成")
            hypit.get(root,build_id,"final.video",target)
        media.verify(target,job["duration"])
        store.update(jid,status="completed",video=str(target),error=None)
        store.event(jid,"视频已完成")
    except Exception as exc:
        message=str(exc)[-900:]
        attention=bool(store.get(jid).get("submission_intent")) or (root/"image-submission-intent.json").exists() or any(term in message for term in ["SUBMISSION_UNCERTAIN","状态不确定","回执无","回执没有","等待超时"])
        store.update(jid,status="needs_attention" if attention else "failed",error=message)
        store.event(jid,"任务暂停："+message)


def execute_claim(job):
    jid=job['id'];token=job['execution']['token']
    finished=threading.Event()
    def heartbeat():
        while not finished.wait(15):
            try:store.renew(jid,token)
            except Exception:
                # The next state write or tool dispatch refuses an expired token.
                traceback.print_exc()
                return
    pulse=threading.Thread(target=heartbeat,daemon=True,name='hypit-lease')
    with execution.scope(jid,token):
        try:
            store.assert_execution(jid)
            pulse.start()
            process(job)
        except execution.ExecutionLost:
            pass  # Never let an obsolete executor overwrite its successor.
        except Exception:
            traceback.print_exc()
            try:store.update(jid,status='needs_attention',error='任务执行意外中断，原提交记录已保留，请核对后继续。')
            except execution.ExecutionLost:pass
        finally:
            finished.set()
            if pulse.is_alive():pulse.join()
            store.release(jid,token)


def loop():
    store.recover_queue()
    with ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_JOBS,
                            thread_name_prefix='hypit-job') as pool:
        running = {}
        while not STOP.is_set():
            for future, jid in list(running.items()):
                if future.done():
                    del running[future]
                    try:
                        future.result()
                    except Exception:
                        # A task must not take the whole queue down on an unexpected failure.
                        traceback.print_exc()
            while not STOP.is_set() and len(running) < config.MAX_CONCURRENT_JOBS:
                job = store.claim(exclude=set(running.values()))
                if not job:
                    break
                running[pool.submit(execute_claim, job)] = job['id']
            STOP.wait(.5)


def start():
    STOP.clear()
    thread=threading.Thread(target=loop,daemon=True,name="hypit-job-worker")
    thread.start()
    return thread
