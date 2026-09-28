"""Read/download an already accepted Wan task. This module has no submit API."""
import json
from urllib.parse import quote
import requests
from . import config, media
from .errors import WorkflowError, classify


def poll(root, profile, task_id):
    doc=json.loads(profile.read_text())
    name=doc['bindings']['@hypit/seedance@1#seedance-2']
    endpoint=doc['endpoints'][name]
    if endpoint['use']!='@local/provider-autodl-wan':
        raise ValueError('该供应商尚未实现直接查询恢复')
    base=endpoint['config']['baseUrl'].rstrip('/')
    try:
        with config.http_session(base) as http:
            response=http.get(base+'/api/v1/tasks/'+quote(task_id,safe=''),
                              headers={'Authorization':'Bearer '+config.video_api_key()},timeout=(15,60))
        if not response.ok:raise WorkflowError(classify('',stage='poll',provider='video',http_status=response.status_code))
        output=response.json().get('output',{})
        if output.get('task_id') not in (None,task_id):raise ValueError('查询回执任务编号不匹配')
        state=output.get('task_status','').upper()
        if state not in ('PENDING','RUNNING','QUEUED','SUCCEEDED','FAILED','CANCELED','CANCELLED'):
            raise ValueError('供应商查询返回未知状态，保留原编号')
        if state=='SUCCEEDED' and not output.get('video_url'):raise ValueError('成功回执缺少结果地址')
        return {'status':state,'url':output.get('video_url'),'code':output.get('code')}
    except requests.RequestException as exc:
        raise WorkflowError(classify(exc,stage='poll',provider='video')) from None


def download(url,target):
    temporary=target.with_name(target.stem+'.download.mp4')
    try:
        with config.http_session(url) as http, http.get(url,stream=True,timeout=(15,90)) as response:
            if not response.ok:raise WorkflowError(classify('',stage='download',provider='video',http_status=response.status_code))
            total=0
            with temporary.open('wb') as out:
                for chunk in response.iter_content(1024*1024):
                    total+=len(chunk)
                    if total>500*1024*1024:raise ValueError('视频下载超过500MB限制')
                    out.write(chunk)
        media.verify(temporary,float(media.probe(temporary)['format']['duration']))
        temporary.replace(target)
    except requests.RequestException as exc:
        raise WorkflowError(classify(exc,stage='download',provider='video')) from None
    finally:
        temporary.unlink(missing_ok=True)
