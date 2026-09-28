"""Generate one shared subject reference only when the plan calls for it."""
import base64
import io
import json
import hashlib
from shutil import copy2
from pathlib import Path

import requests
from PIL import Image

from . import config

MODEL="doubao-seedream-4-0-250828"


def create_shared_subject(root:Path,prompt:str,ratio:str):
    version=hashlib.sha256(json.dumps([MODEL,prompt,ratio],ensure_ascii=False).encode()).hexdigest()[:20]
    target=root/"assets"/f"shared-subject-{version}.jpg"
    if target.is_file():
        copy2(target,root/'assets/shared-subject.jpg')
        return target
    marker=root/"image-submission-intent.json"
    if marker.is_file():
        raise RuntimeError("SUBMISSION_UNCERTAIN: 生图请求已发出但没有保存结果，请人工核对")
    marker.write_text(json.dumps({"model":MODEL,"prompt":prompt},ensure_ascii=False))
    request={"model":MODEL,"prompt":f"单一主体形象设定图，供短视频多段生成保持外观统一。{prompt}。画面比例{ratio}。主体清晰完整、背景简洁，不要文字水印。","size":"2K","response_format":"b64_json","watermark":False}
    response=requests.post(config.BASE+"/images/generations",headers={"Authorization":"Bearer "+config.api_key()},json=request,timeout=(15,180))
    if response.status_code in (400,401,403,404,422):
        marker.unlink(missing_ok=True)
        raise RuntimeError(f"生图服务拒绝请求 HTTP {response.status_code}："+response.text[:400])
    if not response.ok:raise RuntimeError(f"生图服务状态不确定 HTTP {response.status_code}")
    data=response.json()
    encoded=data.get("data",[{}])[0].get("b64_json")
    if not encoded:raise RuntimeError("生图没有返回可保存的图像数据，已暂停")
    raw=base64.b64decode(encoded,validate=True)
    image=Image.open(io.BytesIO(raw))
    image.verify()
    image=Image.open(io.BytesIO(raw)).convert("RGB")
    target.parent.mkdir(parents=True,exist_ok=True)
    image.save(target,format="JPEG",quality=93)
    copy2(target,root/'assets/shared-subject.jpg')
    marker.unlink(missing_ok=True)
    return target
