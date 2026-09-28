"""Stop only the local supervisor recorded by start-local.sh."""
import json
import os
import signal
import subprocess
from app import config

def main():
    path=config.DATA/'local-service.json'
    if not path.is_file():raise SystemExit('未发现本地启动记录。')
    pid=json.loads(path.read_text())['supervisor']
    command=subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True).stdout
    if '-m app.local_service' not in command:raise SystemExit('原服务已退出，未发送停止信号。')
    os.kill(pid,signal.SIGTERM)
    print('已通知本地服务保存进度并停止。')

if __name__=='__main__':main()
