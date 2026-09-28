"""Optional per-user token login for an internal HTTPS deployment."""
import base64
import hashlib
import hmac
import json
import os
import time
import secrets
from . import store

from fastapi import HTTPException, Request


def users():
    raw=os.getenv("VIDEO_AGENT_USERS_JSON","")
    if not raw:
        with store.connect() as c:
            rows=c.execute('SELECT name,token_hash FROM users WHERE enabled=1').fetchall()
        return {r['name']:r['token_hash'] for r in rows}
    result=json.loads(raw)
    if not isinstance(result,dict) or not result or any(not isinstance(k,str) or not isinstance(v,str) or not k or len(v)<20 for k,v in result.items()):
        raise RuntimeError("VIDEO_AGENT_USERS_JSON 格式无效；每名用户需配置不少于20字符的随机访问令牌")
    return result


def secret():
    value=os.getenv("VIDEO_AGENT_SESSION_SECRET","")
    if len(value)<32:raise RuntimeError("多人模式需要配置至少32字符的 VIDEO_AGENT_SESSION_SECRET")
    return value.encode()

def required():
    if os.getenv('VIDEO_AGENT_ENV')=='production' or os.getenv('VIDEO_AGENT_USERS_JSON'):return True
    with store.connect() as c:
        return bool(c.execute('SELECT name FROM users LIMIT 1').fetchone())


def login(username,token):
    expected=users().get(username)
    matched=False
    if expected:
        if expected.startswith('scrypt$'):
            _,salt,value=expected.split('$')
            matched=hmac.compare_digest(hashlib.scrypt(token.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex(),value)
        else:matched=hmac.compare_digest(expected,token)
    if not matched:
        raise HTTPException(401,"账号或访问令牌错误")
    exp=int(time.time()+24*3600)
    body=base64.urlsafe_b64encode(json.dumps([username,exp]).encode()).decode().rstrip("=")
    signature=hmac.new(secret()+expected.encode(),body.encode(),hashlib.sha256).hexdigest()
    return body+"."+signature


def actor(request:Request):
    configured=users()
    if not configured:
        if required():raise HTTPException(401,'当前没有可登录账号，请管理员检查账号配置')
        return 'local'
    cookie=request.cookies.get("hypit_session","")
    try:
        body,signature=cookie.split(".",1)
        username,exp=json.loads(base64.urlsafe_b64decode(body+"="*(-len(body)%4)))
        if username not in configured or int(exp)<time.time():raise ValueError()
        correct=hmac.new(secret()+configured[username].encode(),body.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(correct,signature):raise ValueError()
        with store.connect() as c:
            revoked=c.execute('SELECT expires FROM revoked_sessions WHERE digest=?',(hashlib.sha256(cookie.encode()).hexdigest(),)).fetchone()
        if revoked:raise ValueError()
        return username
    except Exception:
        raise HTTPException(401,"请先登录")


def require_owned(doc,username):
    if doc.get("owner","local")!=username:
        raise HTTPException(404,"任务或素材不存在")


def set_user(name,token=None,role='employee',enabled=True):
    if not name or len(name)>100 or role not in ('employee','admin'):raise ValueError('账号或角色无效')
    if token is not None and len(token)<20:raise ValueError('访问令牌至少20字符')
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        prior=c.execute('SELECT token_hash FROM users WHERE name=?',(name,)).fetchone()
        if token is None:
            if not prior:raise ValueError('新账号需要访问令牌')
            encoded=prior['token_hash']
        else:
            salt=secrets.token_bytes(16)
            encoded='scrypt$'+salt.hex()+'$'+hashlib.scrypt(token.encode(),salt=salt,n=16384,r=8,p=1).hex()
        c.execute('INSERT INTO users VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET token_hash=excluded.token_hash,role=excluded.role,enabled=excluded.enabled',
                  (name,encoded,role,int(enabled)))


def require_admin(username):
    # Local development remains usable; production cannot use an anonymous admin.
    if username=='local' and not required():return
    if os.getenv('VIDEO_AGENT_USERS_JSON'):
        if username in os.getenv('VIDEO_AGENT_ADMINS','').split(','):return
    else:
        with store.connect() as c:r=c.execute('SELECT role,enabled FROM users WHERE name=?',(username,)).fetchone()
        if r and r['enabled'] and r['role']=='admin':return
    raise HTTPException(403,'需要管理员权限')


def login_attempt(identity,limit=10):
    # Hash IP/name before persistence; no passwords or raw client addresses in this table.
    key=hashlib.sha256(identity.encode()).hexdigest()
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute('SELECT failures,reset_at FROM login_limits WHERE identity=?',(key,)).fetchone()
        now=time.time()
        count=r['failures'] if r and r['reset_at']>now else 0
        if count>=limit:raise HTTPException(429,'登录尝试过多，请15分钟后重试')
        reset=r['reset_at'] if count else now+900
        c.execute('INSERT INTO login_limits VALUES (?,?,?) ON CONFLICT(identity) DO UPDATE SET failures=excluded.failures,reset_at=excluded.reset_at',(key,count+1,reset))

def revoke(cookie):
    if not cookie:return
    with store.connect() as c:
        c.execute('DELETE FROM revoked_sessions WHERE expires<?',(time.time(),))
        c.execute('INSERT INTO revoked_sessions VALUES (?,?) ON CONFLICT(digest) DO NOTHING',(hashlib.sha256(cookie.encode()).hexdigest(),time.time()+86400))


def audit(actor,action,target):
    import uuid
    with store.connect() as c:
        c.execute('INSERT INTO audit_events VALUES (?,?,?,?,?)',(uuid.uuid4().hex,actor,action,target,time.time()))
