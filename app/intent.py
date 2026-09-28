"""Normalize optional inputs without presenting inferred intent as user text."""
from . import store

RATIOS = {"16:9": 16/9, "9:16": 9/16, "1:1": 1}

def resolve(data):
    prompt = (data.get("prompt") or "").strip()
    images = data.get("images") or []
    reference = store.asset(data["reference"]) if data.get("reference") else None
    if not (prompt or images or reference):
        raise ValueError("请至少提供文字、图片或参考视频中的一项")
    inferred = ""
    if not prompt:
        if reference and images:
            inferred = "根据参考视频制作新视频。判断上传图片的用途，若为目标主体，用它替换原片主体并保持参考的事件、镜头、动作和节奏；对应不清楚时询问。"
        elif reference:
            inferred = "根据参考视频制作相似的新视频，保留主要事件、镜头、动作、节奏和风格。"
        elif images:
            inferred = "根据图片中的主要内容制作一条展示短片，保持主体身份，自行安排适合它的画面。"
    duration = data.get("duration")
    mode = "fixed" if duration is not None else "reference" if reference else "auto"
    if duration is None and reference:
        duration = reference["duration"]
    ratio = data.get("ratio")
    if not ratio:
        if reference:
            aspect = reference["width"] / reference["height"]
            ratio = min(RATIOS, key=lambda r: abs(RATIOS[r]-aspect))
        else:
            ratio = "9:16"
    policy={'mode':data.get('timing_mode','strict')}
    if policy['mode']=='approximate' and duration is not None:
        policy.update(min=max(1,duration-1),max=min(180,duration+1))
    return {**data, "prompt": prompt, "inferred_intent": inferred,'timing_policy':policy,
            "effective_prompt": prompt or inferred, "duration": duration,
            "duration_mode": mode, "ratio": ratio,
            "reference_mode": "recreate" if reference else "create"}
