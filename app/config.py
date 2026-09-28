import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from urllib.request import getproxies
ROOT = Path(__file__).resolve().parent.parent
def _source_version():
    import hashlib
    digest=hashlib.sha256()
    for directory,pattern in (('app','*.py'),('static','*.js'),('provider-autodl-wan','*.js')):
        for path in sorted((ROOT/directory).glob(pattern)):
            digest.update(str(path.relative_to(ROOT)).encode());digest.update(path.read_bytes())
    return digest.hexdigest()
SOURCE_VERSION = _source_version()
DATA = Path(os.getenv("VIDEO_AGENT_DATA", str(ROOT / "data"))).resolve()
FFMPEG = os.getenv("FFMPEG_PATH", "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg" if Path('/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg').is_file() else shutil.which('ffmpeg') or 'ffmpeg')
FFPROBE = os.getenv("FFPROBE_PATH", "/opt/homebrew/opt/ffmpeg-full/bin/ffprobe" if Path('/opt/homebrew/opt/ffmpeg-full/bin/ffprobe').is_file() else shutil.which('ffprobe') or 'ffprobe')
HYPIT = Path(os.getenv("HYPIT_CLI", str(ROOT/"node_modules/@hypit/hypit/bin/hypit.mjs")))
MODEL = os.getenv("AGENT_MODEL", "doubao-seed-2-0-lite-260215")
AUDIO_MODEL = os.getenv("AGENT_AUDIO_MODEL", "doubao-seed-2-0-lite-260428")
BASE = os.getenv('VIDEO_AGENT_ARK_BASE',"https://ark.cn-beijing.volces.com/api/v3").rstrip('/')
VIDEO_BASE = os.getenv('VIDEO_AGENT_VIDEO_BASE',"https://www.autodl.art/api/v1/ali").rstrip('/')
MAX_UPLOAD = 150 * 1024 * 1024
DSH = Path(os.getenv("DSH_BIN", str(ROOT/"node_modules/.bin/dsh")))
MAX_AGENT_ROUNDS = int(os.getenv("VIDEO_AGENT_MAX_ROUNDS", "16"))
MAX_TOOL_CALLS = int(os.getenv("VIDEO_AGENT_MAX_TOOLS", "160"))
ENABLE_REVISIONS = os.getenv('VIDEO_AGENT_ENABLE_REVISIONS','0')=='1'
# One HTTP process owns the queue; independent jobs can overlap inside it.
MAX_CONCURRENT_JOBS = max(1, min(4, int(os.getenv('VIDEO_AGENT_CONCURRENT_JOBS', '2'))))
def runtime_settings():
    import json
    path=DATA/"runtime-settings.json"
    return json.loads(path.read_text()) if path.is_file() else {}
def media_base_url():
    return os.getenv("VIDEO_AGENT_MEDIA_BASE_URL", runtime_settings().get("media_base_url", "")).rstrip("/")
def proxy_bypass_hosts():
    explicit=os.getenv('VIDEO_AGENT_PROXY_BYPASS_HOSTS')
    values=explicit.split(',') if explicit is not None else runtime_settings().get('proxy_bypass_hosts',[])
    return [host.strip().lower() for host in values if isinstance(host,str) and host.strip()]
def http_session(url):
    import requests
    from urllib.parse import urlsplit
    session=requests.Session()
    if (urlsplit(url).hostname or '').lower() in proxy_bypass_hosts():
        session.trust_env=False
    return session
def media_storage():
    return Path(os.getenv('VIDEO_AGENT_MEDIA_DATA',str(DATA))).resolve()
def media_secret_path():
    import secrets
    media_storage().mkdir(parents=True,exist_ok=True)
    path=media_storage()/"media-signing-secret"
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"w") as f:f.write(secrets.token_hex(32))
    except FileExistsError:pass
    return path
def api_key():
    key = os.getenv("ARK_API_KEY")
    if key: return key
    file = Path(os.getenv("VIDEO_AGENT_ENV_FILE", str(ROOT.parent/"OpenMontage/.env")))
    if file.exists():
        for line in file.read_text().splitlines():
            if line.startswith("ARK_API_KEY="):
                return line.split("=",1)[1].strip().strip('"').strip("'")
    raise RuntimeError("未配置 ARK_API_KEY")
def video_api_key():
    key = os.getenv("AUTODL_API_KEY")
    if key: return key
    file = Path(os.getenv("VIDEO_AGENT_VIDEO_KEY_FILE", str(DATA / "autodl-video-key")))
    if file.is_file():
        key = file.read_text().strip()
        if key: return key
    raise RuntimeError("未配置 AUTODL_API_KEY 或私有视频 Key 文件")
@lru_cache(maxsize=4)
def _node_supports_proxy(node):
    if not node:
        return False
    try:
        result = subprocess.run([node, '--help'], capture_output=True, text=True, timeout=5)
        return result.returncode == 0 and '--use-env-proxy' in result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False

def _proxy_environment(env):
    # Node does not automatically read macOS System Settings, unlike urllib.
    proxies = getproxies()
    for scheme in ('http', 'https'):
        upper, lower = scheme.upper() + '_PROXY', scheme + '_proxy'
        if upper not in env and lower not in env and proxies.get(scheme):
            env[upper] = proxies[scheme]
    if not any(env.get(k) for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')):
        return
    bypass = env.get('no_proxy', env.get('NO_PROXY', proxies.get('no', '')))
    entries = [entry.strip() for entry in bypass.split(',') if entry.strip()]
    for host in ('localhost', '127.0.0.1', '::1', *proxy_bypass_hosts()):
        host=host.strip()
        if not host:continue
        if host not in entries:
            entries.append(host)
    env['NO_PROXY'] = env['no_proxy'] = ','.join(entries)
    if _node_supports_proxy(shutil.which('node', path=env.get('PATH'))):
        options = env.get('NODE_OPTIONS', '')
        if '--use-env-proxy' not in options.split():
            env['NODE_OPTIONS'] = (options + ' --use-env-proxy').strip()

def environment():
    from . import execution
    env = os.environ.copy()
    env.update(execution.environment())
    env.update(ARK_API_KEY=api_key())
    try:
        env["AUTODL_API_KEY"] = video_api_key()
    except RuntimeError:
        pass
    env["PATH"] = str(Path(FFMPEG).parent)+os.pathsep+env.get("PATH","")
    _proxy_environment(env)
    return env
