"""Small, version-pinned CLI bridge. Never interprets model text as commands."""
import json
import subprocess
import os
from pathlib import Path

from . import config
from .errors import classify, WorkflowError


def setup(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    local = root / "node_modules" / "@local"
    local.mkdir(parents=True, exist_ok=True)
    link = local / "provider-autodl-wan"
    if not link.exists():
        # New projects keep immutable adapter bytes. Updating the application
        # must not silently change an accepted request's poll or wire contract.
        import hashlib
        from shutil import copytree,ignore_patterns
        source=config.ROOT/'provider-autodl-wan'
        files=sorted([*source.glob('*.js'),source/'package.json'])
        version=hashlib.sha256(b''.join(p.name.encode()+p.read_bytes() for p in files)).hexdigest()
        pinned=config.DATA/'provider-versions'/version
        if not pinned.is_dir():
            import uuid
            temporary=pinned.with_name(version+'.'+uuid.uuid4().hex+'.tmp')
            try:
                copytree(source,temporary,ignore=ignore_patterns('node_modules','.*'))
                (temporary/'node_modules').mkdir()
                (temporary/'node_modules/@hypit').symlink_to(config.ROOT/'node_modules/@hypit',target_is_directory=True)
                try:temporary.rename(pinned)
                except OSError:
                    if not pinned.is_dir():raise
            finally:
                if temporary.exists():
                    from shutil import rmtree
                    rmtree(temporary)
        link.symlink_to(pinned,target_is_directory=True)
    profile = root / "hypit.runtime.json"
    # Keep the task's provider binding. Refresh only the temporary media gateway.
    if profile.is_file():
        doc = json.loads(profile.read_text())
        changed = False
        for endpoint in doc.get('endpoints', {}).values():
            settings = endpoint.get('config', {})
            if 'mediaBaseUrl' in settings and settings['mediaBaseUrl'] != config.media_base_url():
                settings['mediaBaseUrl'] = config.media_base_url()
                changed = True
        if changed:
            profile.write_text(json.dumps(doc, ensure_ascii=False, indent=2))
        return profile
    doc = {
      "format": "hypit.runtime-local@1",
      "dataRoot": ".hypit/execution",
      "credentials": {"env": {"use": "@hypit/credential-store-env"}},
      "endpoints": {
        "autodl-wan": {"use": "@local/provider-autodl-wan", "config": {
          "baseUrl": config.VIDEO_BASE, "apiKey": {"store": "env", "key": "AUTODL_API_KEY"},
          "mediaBaseUrl":config.media_base_url(),
          "referenceDirectory":str(config.media_storage()/"reference-blobs"),
          "mediaSecretFile":str(config.media_secret_path()),
          "journalDirectory": str(root / "submission-journal")}},
        "media.local": {"use": "@hypit/provider-media-local", "config": {
          "ffmpegPath": config.FFMPEG, "ffprobePath": config.FFPROBE}},
        "hyperframes.local": {"use": "@hypit/provider-hyperframes-local", "config": {
          "workers": 2, "ffmpegPath": config.FFMPEG, "ffprobePath": config.FFPROBE,
          **({'chromePath':os.environ['VIDEO_AGENT_CHROME_PATH']} if os.getenv('VIDEO_AGENT_CHROME_PATH') else {})}}
      },
      "bindings": {"@hypit/seedance@1#seedance-2": "autodl-wan"}
    }
    profile.write_text(json.dumps(doc, ensure_ascii=False, indent=2))
    return profile


def snapshot(root: Path, profile: Path):
    import hashlib
    raw = profile.read_bytes()
    path = root / ('hypit-runtime-' + hashlib.sha256(raw).hexdigest()[:20] + '.json')
    if not path.exists():
        path.write_bytes(raw)
    return path


def call(root: Path, args, timeout=180):
    if not config.HYPIT.is_file():
        raise RuntimeError("Hypit CLI 未安装到固定路径")
    try:
        run = subprocess.run(["node", str(config.HYPIT), *map(str,args), "--json"],
            cwd=root, env=config.environment(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise WorkflowError(classify(exc,stage=str(args[0]),provider='hypit',submission=args[0]=='build')) from None
    try:
        result=json.loads(run.stdout.strip())
    except json.JSONDecodeError:
        if args[0]=='status':
            raise WorkflowError(classify('INVALID_RESPONSE',stage='poll',provider='hypit')) from None
        raise RuntimeError("Hypit 未返回 JSON："+(run.stdout+run.stderr)[-1000:])
    # A failed Build is a successful status query with CLI exit code 1.
    # Keep the structured facts so the supervisor can classify and recover it.
    status_result = (args[0] == 'status' and result.get('format') == 'hypit.cli-status@1'
                     and result.get('build', {}).get('id') == args[1])
    if run.returncode and not status_result:
        raise RuntimeError("Hypit 执行失败："+json.dumps(result,ensure_ascii=False)[-1800:])
    return result


def check(root: Path, run: Path):
    # Checking Run resolves and checks its actual author source, regardless of basename.
    return call(root,["check",run,"--workspace",root])


def plan(root: Path, run: Path, profile: Path):
    return call(root,["plan",run,"--workspace",root,"--runtime",profile])


def build(root: Path, run: Path, profile: Path):
    from . import execution,store
    current=execution.identity()
    if current:store.assert_execution(current[0])
    return call(root,["build",run,"--workspace",root,"--runtime",profile],timeout=240)


def status(root: Path, build_id: str, profile: Path):
    return call(root,["status",build_id,"--workspace",root,"--runtime",profile])


def get(root: Path, build_id: str, output: str, target: Path):
    return call(root,["get",build_id,"--output",output,"--to",target,"--workspace",root])


def builds(root: Path):
    return call(root,["builds","--workspace",root]).get("builds",[])
