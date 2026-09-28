"""Expire only signed delivery copies, never original assets or task evidence.

Default is a report. --apply requires a stopped worker and no active/unknown jobs.
"""
import argparse
import re
import time
from app import config,runtime,store


def candidates(retention_days=7):
    if retention_days<7:raise ValueError('签名素材副本至少保留7天，超过当前最长48小时签名有效期')
    cutoff=time.time()-retention_days*86400
    return [p for p in (config.media_storage()/'reference-blobs').glob('*')
        if re.fullmatch(r'[a-f0-9]{64}\.(mp4|wav|mp3)',p.name) and p.is_file() and not p.is_symlink()
        and p.stat().st_mtime<cutoff]


def cleanup(*,apply=False,retention_days=7):
    paths=candidates(retention_days)
    if apply:
        if runtime.worker_alive():raise ValueError('请先停止 Worker，等待心跳过期后再清理')
        jobs=store.list_jobs(limit=None,include_archived=True)
        if any(j['status'] in (*store.RUNNING_STATES,'queued') or j.get('agent_submission_intent')
               or j.get('submission_intent') or j.get('submission_uncertain')
               or any(b.get('state')=='pending' for b in j.get('agent_builds',{}).values()) for j in jobs):
            raise ValueError('仍有活动任务或未核对提交，暂不清理素材副本')
        for path in paths:
            if path.stat().st_mtime<time.time()-retention_days*86400:path.unlink()
    return {'mode':'deleted' if apply else 'dry_run','files':len(paths),'retention_days':retention_days}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true');parser.add_argument('--retention-days',type=int,default=7)
    args=parser.parse_args();print(cleanup(apply=args.apply,retention_days=args.retention_days))
