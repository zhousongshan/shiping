"""Stop only the local supervisor recorded by start-local.sh."""
import json
import os
import signal
import subprocess
from app import config

def main():
    path=config.DATA/'local-service.json'
    if not path.is_file():raise SystemExit('未发现本地启动记录。')
    state=json.loads(path.read_text());pid=state['supervisor']
    from app import local_process
    supervisor=state.get('supervisor_identity')
    command=subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True).stdout
    if (local_process.matches(supervisor) if supervisor else '-m app.local_service' in command):
        os.kill(pid,signal.SIGTERM)
        print('已通知本地服务保存进度并停止。')
        return
    stopped=[]
    for child in state.get('child_identities',[]):
        if local_process.matches(child):
            try:os.kill(child['pid'],signal.SIGTERM);stopped.append(child['pid'])
            except ProcessLookupError:pass
    if stopped:print('主监督进程已退出，已通知身份核对一致的残留服务停止：'+','.join(map(str,stopped)))
    else:print('没有可确认属于此启动记录的运行进程，未发送停止信号。')

if __name__=='__main__':main()
