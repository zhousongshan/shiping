"""Resume rejected media-fetch submissions without repeating accepted generations."""
import time
import json
from . import config, store, agent_tools, hypit_adapter as hypit
from .errors import classify, Failure

RETRY_DELAYS = (10, 30)


class RecoveryPaused(RuntimeError):
    pass

class WorkDeferred(RuntimeError):
    """Saved a future poll time; no local execution slot needs to stay occupied."""
    pass

def fetch_rejected(build):
    """Only the explicit pre-submission HTTP 400 is safe to resubmit automatically."""
    failed = [op for op in build.get('operations', []) if op.get('state') == 'failed']
    if not failed:
        return False
    for op in failed:
        message = str((op.get('failure') or {}).get('message', ''))
        if (op.get('endpoint') != 'ark' or op.get('receipt') or op.get('handle')
                or 'submit failed: Ark HTTP 400 ' not in message
                or '"code":"InvalidParameter"' not in message.replace(' ', '')
                or 'timeout while fetching resource' not in message
                or not any(field in message for field in ('.video_url', '.audio_url', '.image_url'))):
            return False
    return True


def resume_accepted_poll(job, root, stamp, build, current, profile):
    """Reattach to a receipted provider task after a terminal local poll error."""
    failure = str(current.get('failure') or '')
    if not any(stage in failure for stage in ('poll failed:','collect failed:')) or not classify(failure, stage='poll').retryable:
        return False
    if build.get('recovery_attempts', 0) >= len(RETRY_DELAYS):
        return False
    receipts = [op.get('receipt', {}).get('id') for op in current.get('operations', [])
                if isinstance(op.get('receipt'), dict)]
    receipts = [value for value in receipts if value]
    if len(set(receipts)) != 1:
        return False
    receipt_id = receipts[0]
    # The provider journal is the guarantee that rebuilding this unchanged
    # source will reuse the accepted task rather than issue another POST.
    journal = root / 'submission-journal'
    found = False
    for path in journal.glob('*.json'):
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if entry.get('state') == 'submitted' and entry.get('id') == receipt_id:
            found = True
            break
    if not found or agent_tools.build_stamp(root, build['run'], build['output'],
                                             version=build.get('stamp_version', 1)) != stamp:
        return False
    runtime=json.loads(profile.read_text()) if profile.is_file() else {}
    binding=runtime.get('bindings',{}).get('@hypit/seedance@1#seedance-2')
    if runtime.get('endpoints',{}).get(binding,{}).get('use')=='@local/provider-autodl-wan':
        build.update(provider_task_id=receipt_id,provider_recovery=True,state='pending',
                     recovery_attempts=build.get('recovery_attempts',0)+1)
        store.update(job['id'],agent_builds=job['agent_builds'])
        store.event(job['id'],'已接管原供应商任务的查询，不创建新的生成任务')
        return True
    attempts = build.get('recovery_attempts', 0)
    store.update(job['id'], agent_submission_intent={
        'stamp': stamp, 'run': build['run'], 'output': build['output'],
        'created': time.time(), 'recovery': True, 'receipt_id': receipt_id})
    accepted = hypit.build(root, root / build['run'], profile)
    new_id = (accepted.get('build') or {}).get('id')
    if not new_id or new_id == build['build_id']:
        raise RecoveryPaused('原供应商任务已受理，但重新接管查询没有返回新的本地任务号；请核对，不能重新提交。')
    build.update(build_id=new_id, state='pending', poll_started_at=time.time(),
                 recovery_attempts=attempts + 1,
                 recovery_history=build.get('recovery_history', []) + [build['build_id']])
    build.pop('failure', None)
    store.update(job['id'], agent_builds=job['agent_builds'], agent_submission_intent=None)
    store.event(job['id'], '查询连接中断，已使用原供应商任务号继续查询，没有重新提交生成')
    return True


def wait_builds(job, stop, once=False):
    if job.get('agent_submission_intent'):
        raise RecoveryPaused('上次提交状态尚未确认，请先核对提交状态，避免重复生成。')
    root = config.DATA / 'jobs' / job['id'] / 'project'
    builds = job.get('agent_builds', {})
    pending = [(stamp, b) for stamp, b in builds.items() if b.get('state') in ('pending', 'failed')]
    if not pending:
        return False
    if job.get('retry_requested'):
        for _, b in pending:
            b['recovery_attempts'] = 0
            b['poll_started_at'] = time.time()
        store.update(job['id'], retry_requested=False, agent_builds=builds)
    store.update(job['id'], status='waiting_build')
    deadline = time.monotonic() + 40 * 60
    while pending:
        if stop.is_set():
            raise RuntimeError('服务正在停止，生成回执已保存')
        for stamp, b in list(pending):
            if not b.get('poll_started_at'):
                b['poll_started_at']=time.time()
                store.update(job['id'],agent_builds=builds)
            if once and time.time()-b['poll_started_at']>40*60:
                raise RecoveryPaused('生成服务等待超过40分钟，原任务编号已保留；核对后继续只查询原任务，不重新生成。')
            profile = root/b['runtime_profile'] if b.get('runtime_profile') else hypit.setup(root)
            if b.get('provider_recovery'):
                from . import provider_recovery
                result=provider_recovery.poll(root,profile,b['provider_task_id'])
                if result['status']=='SUCCEEDED':
                    b.update(state='complete',remote_result_url=result['url'],completed_at=time.time())
                    b.pop('failure',None);pending.remove((stamp,b))
                    store.update(job['id'],agent_builds=builds)
                elif result['status'] in ('FAILED','CANCELED','CANCELLED'):
                    b.update(state='failed',failure='供应商任务失败：'+str(result.get('code') or result['status']))
                    store.update(job['id'],agent_builds=builds)
                    raise RecoveryPaused(b['failure'])
                continue
            view = hypit.status(root, b['build_id'], profile)
            current = view.get('build') or {}
            receipts={op['receipt']['id'] for op in current.get('operations',[]) if isinstance(op.get('receipt'),dict) and op['receipt'].get('id')}
            if len(receipts)==1:b['provider_task_id']=next(iter(receipts))
            work = current.get('work') or {}
            outcome = work.get('outcome') or (current.get('result') or {}).get('state')
            if outcome == 'complete':
                b['state'] = 'complete';b['completed_at']=time.time()
                b.pop('failure', None)
                pending.remove((stamp, b))
                store.update(job['id'], agent_builds=builds)
            elif outcome in ('failed', 'cancelled', 'interrupted') or work.get('state') == 'done':
                if resume_accepted_poll(job, root, stamp, b, current, profile):
                    continue
                b.update(state='failed', failure=current.get('failure') or outcome)
                b['fetch_retryable'] = fetch_rejected(current)
                store.update(job['id'], agent_builds=builds)
                if not b['fetch_retryable']:
                    messages = ' '.join(str((o.get('failure') or {}).get('message','')) for o in current.get('operations',[]))
                    reference_fetch_failed = any(
                        o.get('endpoint') == 'autodl-wan'
                        and (o.get('failure') or {}).get('code') == 'InvalidParameter'
                        and str((o.get('failure') or {}).get('message','')).startswith('Failed to download ')
                        for o in current.get('operations',[]))
                    failure=(Failure('REFERENCE_FETCH_FAILED','generation','video') if reference_fetch_failed else
                             classify(messages or b['failure'],stage='generation',provider='video',
                                      submission=not any(o.get('receipt') or o.get('handle') for o in current.get('operations',[]))))
                    store.update(job['id'],failure=failure.public())
                    if failure.submission_uncertain:
                        store.update(job['id'],submission_uncertain={'build_id':b['build_id'],'reason':failure.message})
                    raise RecoveryPaused(failure.message)
                attempts = b.get('recovery_attempts', 0)
                if attempts >= len(RETRY_DELAYS):
                    raise RecoveryPaused('参考素材读取超时，已自动重试两次。已有片段已保留，请稍后点击“继续处理”重试失败片段。')
                # Never submit changed authoring as a recovery of an old request.
                if agent_tools.build_stamp(root, b['run'], b['output'],version=b.get('stamp_version',1)) != stamp:
                    raise RecoveryPaused('失败片段的工程或素材已变化，已保留结果，请核对后继续。')
                store.event(job['id'], f'参考素材读取超时，{RETRY_DELAYS[attempts]} 秒后重试失败片段（{attempts + 1}/2）；已完成片段保留')
                if stop.wait(RETRY_DELAYS[attempts]):
                    raise RuntimeError('服务正在停止，恢复进度已保存')
                # Check ownership before recording a new submission intent.
                current_profile = hypit.setup(root)
                binding=json.loads(current_profile.read_text()).get('bindings',{}).get('@hypit/seedance@1#seedance-2')
                if binding and binding!='ark':
                    raise RecoveryPaused('原请求属于火山，当前配置已切换供应商，不能作为同一次网络重试提交。请明确建立新生成版本。')
                retry_profile = hypit.snapshot(root, current_profile)
                previous = b['build_id']
                b['recovery_attempts'] = attempts + 1
                b['recovery_history'] = b.get('recovery_history', []) + [previous]
                # Persist uncertainty before submitting; a crash must not trigger another POST.
                store.update(job['id'], agent_builds=builds, agent_submission_intent={
                    'stamp': stamp, 'run': b['run'], 'output': b['output'],
                    'created': time.time(), 'recovery': True, 'previous_build_id': previous})
                accepted = hypit.build(root, b['run'], retry_profile)
                bid = (accepted.get('build') or {}).get('id')
                if not bid or bid == previous:
                    raise RuntimeError('恢复提交没有返回新的 Build ID，请先核对提交状态')
                b.update(build_id=bid, state='pending', runtime_profile=str(retry_profile.relative_to(root)))
                b.pop('failure', None)
                store.update(job['id'], agent_builds=builds, agent_submission_intent=None)
        if not pending:
            break
        if once:
            store.update(job['id'],status='waiting_build',next_run_at=time.time()+15)
            raise WorkDeferred()
        if time.monotonic() > deadline:
            raise RecoveryPaused('生成服务等待超时，任务编号和已完成片段已保留；继续时先查询原任务。')
        if stop.wait(10):
            raise RuntimeError('服务正在停止，生成回执已保存')
    return True
