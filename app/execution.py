"""Task execution identity, propagated explicitly to task tool subprocesses."""
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar

_current = ContextVar('hypit_execution', default=None)


class ExecutionLost(RuntimeError):
    pass


class ExecutionBusy(ValueError):
    pass


def identity():
    value = _current.get()
    if value is not None:
        return value
    if os.getenv('VIDEO_AGENT_EXECUTION_TOKEN'):
        return (os.environ['VIDEO_AGENT_EXECUTION_JOB'],
                os.environ['VIDEO_AGENT_EXECUTION_TOKEN'])
    return None


@contextmanager
def scope(job_id, token):
    handle = _current.set((job_id, token))
    try:
        yield
    finally:
        _current.reset(handle)


def environment():
    current = identity()
    return ({'VIDEO_AGENT_EXECUTION_JOB': current[0],
             'VIDEO_AGENT_EXECUTION_TOKEN': current[1]} if current else {})


def check(job, now):
    lease = job.get('execution') or {}
    current = identity()
    if current:
        if (current != (job['id'], lease.get('token')) or
                lease.get('expires', 0) <= now):
            raise ExecutionLost('执行权已过期，旧执行器已停止写入和提交')
    elif lease.get('token') and lease.get('expires', 0) > now:
        raise ExecutionBusy('当前制作回合尚未结束，请稍后重试')


@contextmanager
def maintain(job):
    from . import store
    jid=job['id'];token=job['execution']['token'];done=threading.Event()
    def pulse():
        while not done.wait(15):
            try:store.renew(jid,token)
            except Exception:return
    heartbeat=threading.Thread(target=pulse,daemon=True)
    with scope(jid,token):
        try:
            store.assert_execution(jid);heartbeat.start();yield
        finally:
            done.set()
            if heartbeat.is_alive():heartbeat.join()
            store.release(jid,token)
