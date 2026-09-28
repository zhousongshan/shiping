"""Foreground supervisor for the local API, Worker and signed media server."""
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from . import config,local_process


def main():
    config.DATA.mkdir(parents=True,exist_ok=True)
    stop=threading.Event();children=[]
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
    with (config.DATA/'.local-service.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        for port in (4780,4781):
            with socket.socket() as check:
                check.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                try:check.bind(('127.0.0.1',port))
                except OSError:raise RuntimeError(f'本地端口 {port} 已被占用，请先停止旧服务') from None
        env=os.environ.copy();env['VIDEO_AGENT_EMBEDDED_WORKER']='0'
        commands=[['-m','uvicorn','app.server:app','--host','127.0.0.1','--port','4780'],
                  ['-m','app.worker_main'],
                  ['-m','uvicorn','app.media_gateway:app','--host','127.0.0.1','--port','4781']]
        state=config.DATA/'local-service.json'
        def save_state():
            value={'supervisor':os.getpid(),'children':[p.pid for p in children],
                'supervisor_identity':local_process.record(os.getpid()),
                'child_identities':[local_process.record(p.pid) for p in children]}
            temporary=state.with_suffix('.tmp');temporary.write_text(json.dumps(value));temporary.replace(state)
        try:
            for command in commands:
                children.append(subprocess.Popen([sys.executable,*command],cwd=config.ROOT,env=env,start_new_session=True))
            save_state()
            print('本地视频助手：http://127.0.0.1:4780/；API、Worker、素材服务分别运行。',flush=True)
            restarts=[0]*len(children)
            started=[time.monotonic()]*len(children)
            while not stop.wait(1):
                for index,p in enumerate(children):
                    if p.poll() is None:continue
                    if time.monotonic()-started[index]>=60:restarts[index]=0
                    restarts[index]+=1
                    if restarts[index]>5:raise RuntimeError('服务进程连续退出五次，请检查日志后重新启动')
                    print(f'服务进程退出，{min(30,2**restarts[index])}秒后恢复，已有任务记录保留。',flush=True)
                    if stop.wait(min(30,2**restarts[index])):break
                    children[index]=subprocess.Popen([sys.executable,*commands[index]],cwd=config.ROOT,env=env,start_new_session=True)
                    started[index]=time.monotonic()
                    save_state()
        finally:
            for p in children:
                if p.poll() is None:os.killpg(p.pid,signal.SIGTERM)
            for p in children:
                try:p.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid,signal.SIGKILL);p.wait()
            state.unlink(missing_ok=True)


if __name__=='__main__':main()
