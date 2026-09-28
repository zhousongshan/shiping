"""Read exact signed input bytes before submitting a paid video build."""
import hashlib
import hmac
import math
import time
from urllib.parse import urlencode,urlsplit
import requests
from . import config
from .production_schema import artifact,file_hash


class MediaUnavailable(ValueError):
    pass


def verify(root,paths,profile):
    runtime=__import__('json').loads(profile.read_text())
    binding=runtime['bindings']['@hypit/seedance@1#seedance-2']
    settings=runtime['endpoints'][binding]['config']
    files=[p for p in paths if p.suffix.lower() in ('.mp4','.wav','.mp3')]
    if not files:return None
    base=settings.get('mediaBaseUrl','').rstrip('/')
    if urlsplit(base).scheme not in ('http','https') or not urlsplit(base).netloc:
        raise MediaUnavailable('参考素材通道未配置，尚未提交视频生成')
    expires=math.ceil(time.time()/86400)*86400+86400
    records=[]
    for path in files:
        digest=file_hash(path);name=digest+path.suffix.lower()
        from pathlib import Path
        directory=Path(settings['referenceDirectory']);directory.mkdir(parents=True,exist_ok=True)
        target=directory/name
        # Atomic publication: concurrent requests must never expose a partial file.
        import os,tempfile
        with tempfile.NamedTemporaryFile(dir=directory,delete=False) as output:
            temporary=Path(output.name)
            try:
                with path.open('rb') as source:
                    while chunk:=source.read(1024*1024):output.write(chunk)
                output.flush();os.fsync(output.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True);raise
        temporary.replace(target)
        secret=Path(settings['mediaSecretFile']).read_bytes()
        signature=hmac.new(secret,f'{name}:{expires}'.encode(),hashlib.sha256).hexdigest()
        url=base+'/media/'+name+'?'+urlencode({'expires':expires,'signature_value':signature})
        record={'sha256':digest,'bytes':path.stat().st_size,'expires':expires,'checked_at':time.time(),
                'scope':'worker_download_only_not_provider_read','status':'failed'}
        started=time.monotonic()
        try:
            with config.http_session(url) as session,session.get(url,stream=True,timeout=(10,20),allow_redirects=False) as response:
                if response.status_code!=200:raise MediaUnavailable('参考素材下载预检未通过，尚未提交视频生成')
                observed=hashlib.sha256();total=0
                for chunk in response.iter_content(1024*1024):
                    total+=len(chunk)
                    if total>record['bytes'] or time.monotonic()-started>60:
                        raise MediaUnavailable('参考素材下载大小或耗时异常，尚未提交视频生成')
                    observed.update(chunk)
                if total!=record['bytes'] or observed.hexdigest()!=digest:
                    raise MediaUnavailable('公网参考素材与本地文件不一致，尚未提交视频生成')
            record['status']='passed';records.append(record)
        except (requests.RequestException,MediaUnavailable):
            artifact(root,'media-preflight',{'files':records+[record],'status':'failed'})
            raise MediaUnavailable('参考素材下载预检失败，请检查素材服务和公网地址后继续；本次尚未提交视频生成') from None
    return artifact(root,'media-preflight',{'files':records,'status':'passed'})
