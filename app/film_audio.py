"""Optional whole-film soundtrack from an administrator's approved local library.

No downloads or model generation here. Dialogue/effects remain in the source mix;
accurate narration/TTS and subtitles are a separate, not-yet-implemented route.
"""
import json
import math
import os
from pathlib import Path
from . import config,media
from .production_schema import file_hash,fingerprint


class AudioUnavailable(ValueError):
    pass


def library():
    directory=Path(os.getenv('VIDEO_AGENT_MUSIC_DIRECTORY',str(config.DATA/'music-library'))).resolve()
    catalog=directory/'catalog.json'
    if not catalog.is_file():return {}
    entries=json.loads(catalog.read_text())
    result={}
    for item in entries:
        path=(directory/item['file']).resolve()
        if (not path.is_relative_to(directory) or not path.is_file() or path.suffix.lower() not in ('.wav','.mp3','.m4a')
                or item.get('approved_for_company_use') is not True or not item.get('license_note')):
            raise AudioUnavailable('公司配乐库条目不完整，请管理员核对文件和使用许可')
        key=item['id']
        if not isinstance(key,str) or not key or key in result:raise AudioUnavailable('公司配乐库编号必须唯一')
        result[key]={**item,'path':path,'sha256':file_hash(path)}
    return result


def options():
    return [{'id':k,'description':v.get('description','')} for k,v in library().items()]


def validate(plan):
    audio=plan.get('audio_plan') or {'mode':'original'}
    if not isinstance(audio,dict) or audio.get('mode') not in ('original','silent','library'):
        raise ValueError('全片声音模式须为original/silent/library')
    if audio['mode']=='library':
        item=library().get(audio.get('music_id'))
        if not item:raise AudioUnavailable('当前music_id不存在或配乐库为空；请改用original让视频模型生成音乐，不能选择无素材的library；尚未提交视频生成')
        volume=audio.get('music_volume',.18)
        if isinstance(volume,bool) or not isinstance(volume,(int,float)) or not math.isfinite(volume) or not 0<volume<=1:
            raise ValueError('配乐音量须为0至1之间的数值')
        audio={**audio,'music_volume':volume,'music_sha256':item['sha256']}
    plan['audio_plan']=audio


def preflight(job):
    audio=(job.get('plan') or {}).get('audio_plan') or {}
    if audio.get('mode')!='library':return None
    item=library().get(audio.get('music_id'))
    if not item or item['sha256']!=audio.get('music_sha256'):
        raise AudioUnavailable('计划绑定的配乐已变化，请管理员核对后重新规划')
    info=media.probe(item['path'])
    if not any(s.get('codec_type')=='audio' for s in info['streams']):raise AudioUnavailable('配乐文件没有可用音轨')
    if float(info['format']['duration'])+.05<float(job['duration']) and item.get('allow_loop') is not True:
        raise AudioUnavailable('全片配乐时长不足且未允许循环，请管理员补齐素材')
    return item


def compose(job,root,clips):
    audio=(job.get('plan') or {}).get('audio_plan') or {'mode':'original'}
    if audio['mode']=='original':return {}
    if audio['mode']=='silent':return {'mute':True}
    item=preflight(job)
    inputs=[]
    for clip in clips:
        path=(root/clip['path']).resolve()
        if not path.is_relative_to(root.resolve()):raise ValueError('音轨输入超出工程目录')
        inputs.append((path,float(clip['duration'])))
    identity=fingerprint({'audio':audio,'clips':[(file_hash(p),d) for p,d in inputs],'policy':'film-audio-v1'})
    target=root/'assets'/'soundtracks'/f'{identity}.wav';target.parent.mkdir(parents=True,exist_ok=True)
    if target.is_file():return {'audio':str(target.relative_to(root))}
    # The resulting single track spans the complete film. No per-shot music restart.
    command=[config.FFMPEG,'-v','error','-y'];filters=[];labels=[]
    for index,(path,duration) in enumerate(inputs):
        info=media.probe(path)
        if any(s.get('codec_type')=='audio' for s in info['streams']):
            command+=['-i',str(path)]
        else:command+=['-f','lavfi','-i','anullsrc=r=48000:cl=stereo']
        label=f'part{index}';labels.append(f'[{label}]')
        filters.append(f'[{index}:a]aresample=48000,aformat=channel_layouts=stereo,apad,atrim=duration={duration},asetpts=PTS-STARTPTS[{label}]')
    command+=['-stream_loop','-1' if item.get('allow_loop') else '0','-i',str(item['path'])]
    total=sum(d for _,d in inputs)
    filters.append(''.join(labels)+f'concat=n={len(inputs)}:v=0:a=1[original]')
    filters.append(f'[{len(inputs)}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=duration={total},asetpts=PTS-STARTPTS,volume={audio["music_volume"]},afade=t=in:d=0.1,afade=t=out:st={max(0,total-.3)}:d=0.3[music]')
    filters.append('[original][music]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95:latency=1[final]')
    temporary=target.with_suffix('.pending.wav')
    try:
        media.command([*command,'-filter_complex',';'.join(filters),'-map','[final]','-t',str(total),'-c:a','pcm_s16le',str(temporary)],timeout=180)
        actual=float(media.probe(temporary)['format']['duration'])
        if abs(actual-total)>.1:raise AudioUnavailable('全片配乐合成时长不匹配，原片段保留')
        temporary.replace(target)
    finally:temporary.unlink(missing_ok=True)
    return {'audio':str(target.relative_to(root))}
