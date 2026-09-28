"""Identify owned service processes before signalling a stale supervisor record."""
import subprocess


def identity(pid):
    result=subprocess.run(['ps','-p',str(int(pid)),'-o','lstart=','-o','command='],capture_output=True,text=True)
    return result.stdout.strip() if result.returncode==0 else ''


def record(pid):
    return {'pid':pid,'identity':identity(pid)}


def matches(entry):
    return bool(entry.get('identity') and identity(entry['pid'])==entry['identity'])
