"""Production configuration gates and queue leadership."""
import fcntl
import os
import time
from urllib.parse import urlsplit
from . import config, database, store


def production():
    return os.getenv('VIDEO_AGENT_ENV') == 'production'


def embedded_worker():
    return os.getenv('VIDEO_AGENT_EMBEDDED_WORKER', '0' if production() else '1') == '1'


def validate():
    if not production():
        return
    from . import auth
    problems = []
    if not database.url().startswith(('postgresql://', 'postgres://')):
        problems.append('VIDEO_AGENT_DATABASE_URL 必须配置 PostgreSQL')
    if not auth.users():problems.append('必须配置公司账号，不能使用 local 共用身份')
    try:auth.secret()
    except RuntimeError:problems.append('必须配置会话签名密钥')
    if os.getenv('VIDEO_AGENT_COOKIE_SECURE') != '1':problems.append('必须启用 HTTPS 安全 Cookie')
    if urlsplit(os.getenv('VIDEO_AGENT_PUBLIC_URL','')).scheme != 'https':problems.append('必须配置 VIDEO_AGENT_PUBLIC_URL 为 HTTPS 地址')
    media=urlsplit(config.media_base_url())
    if media.scheme!='https' or not media.hostname or media.hostname.endswith('.trycloudflare.com'):
        problems.append('必须配置持久 HTTPS 素材域名')
    if embedded_worker():problems.append('生产 API 和 Worker 必须分别运行')
    for getter,name in ((config.api_key,'理解模型密钥'),(config.video_api_key,'视频模型密钥')):
        try:getter()
        except RuntimeError:problems.append(f'缺少{name}')
    if problems:raise RuntimeError('生产配置未就绪：'+'；'.join(problems))


class QueueLeader:
    """One active scheduler; API instances remain independent.

    This is deliberately a single queue leader until task-level distributed leases
    and shared artifact storage have passed acceptance. Workers overlap jobs inside
    that leader; a second scheduler fails closed instead of recovering live jobs.
    """
    def __enter__(self):
        config.DATA.mkdir(parents=True,exist_ok=True)
        self.file=open(config.DATA/'worker.lock','a')
        self.db=None
        try:
            fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if database.url():
                import psycopg
                self.db=psycopg.connect(database.url(),autocommit=True,connect_timeout=5)
                if not self.db.execute('SELECT pg_try_advisory_lock(47800002)').fetchone()[0]:
                    raise RuntimeError('已有任务调度进程连接此数据库')
        except BaseException:
            if self.db:self.db.close()
            self.file.close()
            raise
        return self

    def heartbeat(self):
        if self.db:self.db.execute('SELECT 1')
        with store.connect() as c:
            c.execute('INSERT INTO service_heartbeat VALUES (?,?) ON CONFLICT(name) DO UPDATE SET updated=excluded.updated',('worker',time.time()))

    def __exit__(self,*exc):
        if self.db:self.db.close()
        self.file.close()


def worker_alive():
    with store.connect() as c:
        row=c.execute('SELECT updated FROM service_heartbeat WHERE name=?',('worker',)).fetchone()
    return bool(row and time.time()-row['updated']<30)
