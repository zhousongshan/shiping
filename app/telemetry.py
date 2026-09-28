"""Append-only step timings, with nesting and no input media or credentials."""
import contextlib
import contextvars
import json
import os
import time
import uuid
from . import config

_parent=contextvars.ContextVar('hypit_trace_parent',default=None)

def append(jid,record):
    try:
        folder=config.DATA/'jobs'/jid
        folder.mkdir(parents=True,exist_ok=True)
        fd=os.open(folder/'step-trace.jsonl',os.O_APPEND|os.O_CREAT|os.O_WRONLY,0o600)
        try:os.write(fd,(json.dumps(record,ensure_ascii=False)+'\n').encode())
        finally:os.close(fd)
    except OSError:
        pass  # Observability must never make an accepted submission run twice.

@contextlib.contextmanager
def step(jid,action):
    sid=uuid.uuid4().hex
    record={'step_id':sid,'parent_id':_parent.get(),'action':action,'started':time.time()}
    append(jid,{**record,'event':'started'})
    token=_parent.set(sid);begin=time.monotonic()
    try:
        yield
    except BaseException as exc:
        record.update(failed=True,error_type=type(exc).__name__)
        raise
    else:
        record['failed']=False
    finally:
        _parent.reset(token)
        append(jid,{**record,'event':'ended','ended':time.time(),'seconds':round(time.monotonic()-begin,3)})
