import subprocess,json,uuid,hashlib
from pathlib import Path
from shutil import copy2
from PIL import Image,ImageOps
from .config import DATA,FFMPEG,FFPROBE
from . import store
def command(args,timeout=90):
    r=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if r.returncode:raise RuntimeError(r.stderr[-1400:] or "媒体处理失败")
    return r.stdout
def probe(path):
    return json.loads(command([FFPROBE,"-v","error","-show_format","-show_streams","-of","json",str(path)]))

def audio_metrics(path):
    """Measure extracted 16-bit PCM; presence, silence and semantics are separate."""
    import array,math,sys,wave
    with wave.open(str(path),'rb') as sound:
        if sound.getsampwidth()!=2 or sound.getcomptype()!='NONE':
            raise ValueError('声音检测需要16位PCM音频')
        samples=array.array('h',sound.readframes(sound.getnframes()))
        if sys.byteorder!='little':samples.byteswap()
    if not samples:raise ValueError('音轨为空，未得到实际音频样本')
    peak=max(abs(x) for x in samples)/32768
    rms=math.sqrt(sum(x*x for x in samples)/len(samples))/32768
    return {'peak_dbfs':round(20*math.log10(peak),2) if peak else None,
            'rms_dbfs':round(20*math.log10(rms),2) if rms else None,
            'near_silent':peak<.001,'silence_threshold_dbfs':-60}
def save_upload(temp:Path, kind:str, name:str, owner:str="local"):
    i=uuid.uuid4().hex;dest=DATA/"assets"/i;dest.mkdir(parents=True)
    if kind=="image":
        with Image.open(temp) as im:
            if im.width*im.height>50_000_000:raise ValueError("图片不能超过5000万像素")
            if min(im.size)<128:raise ValueError("图片过小，请上传清晰图片")
            im=ImageOps.exif_transpose(im).convert("RGB");im.thumbnail((1280,1280))
        target=dest/"image.jpg";im.save(target,quality=92)
        copy2(temp,dest/"original-image")
        info={"width":im.width,"height":im.height}
    elif kind=="video":
        info=probe(temp)
        video=next((s for s in info["streams"] if s["codec_type"]=="video"),None)
        if not video:raise ValueError("文件不包含视频画面")
        seconds=float(info["format"].get("duration",0))
        if not 1<=seconds<=180:raise ValueError("试用版参考视频支持1—180秒")
        target=dest/"reference.mp4"
        original=dest/"original-upload"
        copy2(temp,original)
        command([FFMPEG,"-v","error","-y","-i",str(temp),"-map","0:v:0","-an","-vf","scale=960:960:force_original_aspect_ratio=decrease:force_divisible_by=2","-c:v","libx264","-crf","25",str(target)],timeout=240)
        info={"duration":seconds,"width":video["width"],"height":video["height"],
              "original_path":str(original),"proxy_path":str(target),
              "has_audio":any(s.get("codec_type")=="audio" for s in info["streams"])}
    else:raise ValueError("仅支持图片或视频")
    d={"id":i,"kind":kind,"name":name[:150],"path":str(target),"owner":owner,**info}
    store.put_asset(d);return d

def original_path(asset):
    """Support uploads made before original_path was stored."""
    path=Path(asset.get("original_path") or Path(asset["path"]).parent/"original-upload")
    return path if path.is_file() else Path(asset["path"])
def frames(path,folder,count=8):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    info=probe(path);duration=float(info["format"]["duration"])
    version=hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    result=[]
    for n in range(count):
        t=max(0,min(duration-0.15,duration*(n+.25)/count))
        # Sampling density can change. A cached frame must retain its actual timestamp.
        f=folder/f"frame-{version}-{n}-{t:.6f}.jpg"
        if not f.exists():
            command([FFMPEG,"-v","error","-y","-ss",str(t),"-i",str(path),"-frames:v","1","-vf","scale=640:-2","-pix_fmt","yuvj420p",str(f)])
        if f.exists():result.append((round(t,2),f))
    return result
def verify(path,expected):
    info=probe(path);streams=info["streams"]
    if not any(s["codec_type"]=="video" for s in streams):raise RuntimeError("结果没有视频画面")
    duration=float(info["format"]["duration"])
    if abs(duration-expected)>.15:raise RuntimeError(f"时长不符：期望{expected}秒，实际{duration}秒；请按目标时长合成或校正")
    command([FFMPEG,"-v","error","-i",str(path),"-f","null","-"],timeout=180)
    return {"duration":duration,"bytes":Path(path).stat().st_size}
