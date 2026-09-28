"""Compile a validated, small video plan to fixed Hypit templates."""
from pathlib import Path
from shutil import copy2
from xml.sax.saxutils import escape

from . import store


def xml(value):
    return escape(str(value), {'"': '&quot;', "'": '&apos;'})


def prepare(job, plan, root: Path):
    root.mkdir(parents=True, exist_ok=True)
    assets = root / "assets"
    assets.mkdir(exist_ok=True)
    for aid in job.get("images", []):
        asset = store.asset(aid)
        copy2(asset["path"], assets / f"{aid}.jpg")
    if job.get("reference"):
        ref = store.asset(job["reference"])
        copy2(ref["path"], assets / "reference.mp4")
    (root / "look.svs").write_text('<?svml using="@hypit/svs@1"?>\n<sheet version="1">\nfilm.main { background: #101010; }\nmedia.full { fit: contain; stack-order: 10; playback: once-start; }\n</sheet>\n')
    return assets


def segment_source(job, segment, index: int, root: Path):
    images = segment.get("image_ids", [])
    for aid in images:
        if aid not in job.get("images", []):
            raise ValueError("方案引用了未上传图片")
    shared = root / "assets" / "shared-subject.jpg"
    use_shared = bool(segment.get("use_shared_subject"))
    if use_shared and not shared.is_file():
        raise ValueError("统一主体图片尚未生成")
    head = ['<?svml using="@hypit/markup@1"?>', '<svml>',
            '<import as="text" from="@hypit/text@1"/>',
            '<import as="seedance" from="@hypit/seedance@1"/>']
    for n, aid in enumerate(images):
        head.append('<import as="asset" from="@hypit/media@1"/>' if n == 0 else '')
        head.append(f'<asset:Image id="image{n}" src="./assets/{aid}.jpg"/>')
    if use_shared:
        if not images: head.append('<import as="asset" from="@hypit/media@1"/>')
        head.append('<asset:Image id="shared" src="./assets/shared-subject.jpg"/>')
    prompt = segment["prompt"]
    head.append(f'<text:Value id="direction">{xml(prompt)}</text:Value>')
    props = f'id="clip" model="standard" prompt={{direction}} duration="{int(segment["duration"])}" resolution="720p" aspect-ratio="{xml(job["ratio"])}" generate-audio="true"'
    if images or use_shared:
        head.append(f'<seedance:ReferenceVideo {props}>')
        for n in range(len(images)):
            head.append(f'<seedance:Reference image={{image{n}}}/>')
        if use_shared:head.append('<seedance:Reference image={shared}/>')
        head.append('</seedance:ReferenceVideo>')
    else:
        head.append(f'<seedance:TextVideo {props}/>')
    head.append('</svml>')
    source = root / f'segment-{index}.svml'
    source.write_text('\n'.join(x for x in head if x) + '\n')
    run = root / f'segment-{index}.svrun'
    run.write_text(f'<?svml using="@hypit/run-markup@1"?>\n<svrun version="1"><author source="./{source.name}"/><target output="clip.video"/></svrun>\n')
    return run


def film_source(job, plan, root: Path, durations, audio_path=None,clip_paths=None,has_audio=None):
    if len(durations) != len(plan["segments"]):
        raise ValueError("片段数量不一致")
    clip_paths=clip_paths or [f'assets/clip{i}.mp4' for i in range(len(durations))]
    has_audio=has_audio if has_audio is not None else [True]*len(durations)
    width, height = {"16:9":(1280,720),"9:16":(720,1280),"1:1":(720,720)}[job["ratio"]]
    lines = ['<?svml using="@hypit/markup@1"?>', '<svml>',
        '<import as="asset" from="@hypit/media@1"/>',
        '<import as="look" source="./look.svs"/>',
        '<import as="time" from="@hypit/timeline-author@1"/>',
        '<import as="space" from="@hypit/spatial@1"/>',
        '<import as="pipeline" from="@hypit/media-pipeline@1"/>',
        '<import as="picture" from="@hypit/media-track@1"/>',
        '<import as="audio" from="@hypit/audio-track@1"/>',
        '<import as="film" from="@hypit/film@1"/>',
        '<import as="render" from="@hypit/render-hyperframes@1"/>']
    for i in range(len(durations)):
        lines.append(f'<asset:Video id="clip{i}" src="./{xml(clip_paths[i])}"/>')
    if audio_path:lines.append(f'<asset:Audio id="soundtrack" src="./{xml(audio_path)}"/>')
    boundaries=[round(sum(float(d) for d in durations[:i+1])*25) for i in range(len(durations))]
    total = boundaries[-1]
    lines += ['<time:Clock id="clock" frame-rate="25"/>',
        f'<time:Timeline id="timeline" clock={{clock}} end="{total}f"/>',
        f'<space:Canvas id="canvas" width="{width}" height="{height}"/>',
        '<space:Frame id="full" within={canvas} left="0%" top="0%" right="100%" bottom="100%"/>']
    for i in range(len(durations)):
        policy='default' if has_audio[i] and not audio_path else 'none'
        lines.append(f'<pipeline:Normalize id="media{i}" source={{clip{i}}} video="primary-moving" audio="{policy}" span-authority="video" clock={{clock}}/>')
    lines.append('<picture:Track id="pictures" timeline={timeline.timeline} canvas={canvas}>')
    pos = 0
    for i, d in enumerate(durations):
        end = boundaries[i]
        lines.append(f'<picture:Item id="shot{i}" media={{media{i}.media}} frame={{full}} start="{pos}f" end="{end}f" appearance={{look.media.full}}/>')
        pos = end
    lines.append('</picture:Track>')
    if audio_path:lines.append('<pipeline:Normalize id="music" source={soundtrack} video="none" audio="default" span-authority="audio" clock={clock}/>')
    sounds=bool(audio_path) or any(has_audio)
    if sounds:lines.append('<audio:Track id="sounds" timeline={timeline.timeline}>')
    pos = 0
    for i, d in enumerate([] if audio_path else durations):
        end = boundaries[i]
        if has_audio[i]:lines.append(f'<audio:Item id="sound{i}" source={{media{i}.media}} start="{pos}f" end="{end}f" playback="once" fade-in="2f" fade-out="4f"/>')
        pos = end
    if audio_path:lines.append(f'<audio:Item id="soundtrack-main" source={{music.media}} start="0f" end="{total}f" playback="once" fade-in="2f" fade-out="4f"/>')
    if sounds:lines.append('</audio:Track>')
    lines += [
      '<film:Film id="main" canvas={canvas} timeline={timeline.timeline} appearance={look.film.main}>',
      '<film:Track source={pictures.visual}/>'+('<film:Track source={sounds.audio}/>' if sounds else ''),
      '</film:Film>',
      '<render:Video id="final" composition={main.composition} timeline={timeline.timeline}/>',
      '</svml>']
    (root / "film.svml").write_text('\n'.join(lines) + '\n')
    run = root / "film.svrun"
    run.write_text('<?svml using="@hypit/run-markup@1"?>\n<svrun version="1"><author source="./film.svml"/><target output="final.video"/></svrun>\n')
    return run
