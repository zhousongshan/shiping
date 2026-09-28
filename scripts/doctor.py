"""Read-only checks. Never creates a generation task or prints credentials."""
import argparse
import json
import shutil
import subprocess
import time
from urllib.parse import urlsplit
from app import config, runtime, store


def inspect(connect=False):
    checks={}
    for name,path in [('ffmpeg',config.FFMPEG),('ffprobe',config.FFPROBE),('node','node')]:
        checks[name]={'available':bool(shutil.which(path))}
    checks['hypit']={'available':config.HYPIT.is_file()}
    checks['agent']={'available':config.DSH.is_file()}
    try:
        with store.connect() as c:c.execute('SELECT 1')
        checks['database']={'available':True}
    except Exception:checks['database']={'available':False}
    try:runtime.validate();checks['production_config']={'valid':True,'production':runtime.production()}
    except RuntimeError as e:checks['production_config']={'valid':False,'message':str(e)}
    checks['reference']={'configured':bool(config.media_base_url()),'host':urlsplit(config.media_base_url()).hostname,
                         'external_fetch_verified':False}
    if connect:
        # Exercise the same Node environment as the Harness, without paid inference.
        script='''const t=Date.now();fetch(process.env.PROBE_URL,{headers:{Authorization:"Bearer "+process.env.ARK_API_KEY},signal:AbortSignal.timeout(15000)}).then(r=>console.log(JSON.stringify({http_status:r.status,elapsed_ms:Date.now()-t}))).catch(e=>console.log(JSON.stringify({error:e.name,code:e.cause?.code||null,elapsed_ms:Date.now()-t})));'''
        try:
            env=config.environment();env['PROBE_URL']=config.BASE+'/models'
            r=subprocess.run(['node','-e',script],env=env,capture_output=True,text=True,timeout=20)
            checks['ark_connection']=json.loads(r.stdout)
        except Exception:checks['ark_connection']={'error':'probe_failed'}
    return {'checked_at':time.time(),'checks':checks,'generation_verified':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--connect',action='store_true')
    print(json.dumps(inspect(p.parse_args().connect),ensure_ascii=False,indent=2))
