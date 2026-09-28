"""Build a source-only handoff archive using an explicit allowlist."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from app import config


def export():
    target=config.ROOT/'dist'/'hypit-source.zip';target.parent.mkdir(exist_ok=True)
    files=[]
    for directory in ('app','static','scripts','tests','provider','provider-autodl-wan','agent-tools','deploy','docs'):
        for p in (config.ROOT/directory).rglob('*'):
            if not p.is_file() or any(x in ('__pycache__','node_modules','backups') for x in p.parts):continue
            if p.suffix in ('.py','.js','.ts','.html','.css','.json','.md','.txt','.yaml','.yml','.sh','.svml','.svrun') or p.name in ('Dockerfile','Caddyfile'):
                files.append(p)
    files.extend(config.ROOT/name for name in ('README.md','DEVELOPMENT_HANDOFF.md','requirements.txt','requirements-lock.txt','requirements-dev.txt','package.json','package-lock.json','.gitignore','.dockerignore','.env.example'))
    with ZipFile(target,'w',ZIP_DEFLATED) as z:
        for p in sorted(set(files)):
            if p.is_file():z.write(p,Path('hypit-agent')/p.relative_to(config.ROOT))
    print(target)
    return target


if __name__=='__main__':export()
