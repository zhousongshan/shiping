"""Locate final-film failures, then require fresh unit evidence before repair."""
from .production_schema import fingerprint


def targets(job, review):
    units=[u['id'] for u in job['plan']['units']]
    selected=set()
    for check in review.get('checks',[]):
        if check.get('status')!='fail':continue
        ids=check.get('unit_ids',[])
        if not isinstance(ids,list) or not ids or any(uid not in units for uid in ids):
            return []  # A vague final failure is not authority to regenerate everything.
        selected.update(ids)
    for join in review.get('joins',[]):
        if join.get('verdict')!='fail':continue
        boundaries=(job.get('composition') or {}).get('boundaries',[])
        match=next((i for i,at in enumerate(boundaries) if abs(float(at)-float(join.get('at',-1)))<.05),None)
        if match is None:return []
        selected.update(units[match:match+2])
    return [uid for uid in units if uid in selected]


def key(review):
    return fingerprint({k:review.get(k) for k in ('sha256','context_sha256','verdict','checks','joins')})


def dependency_changed(job, build, dependencies):
    """Changed accepted media forces dependent units to use the new real frame."""
    manifest=job.get('generation_manifests',{}).get(build.get('manifest_id'),{})
    old=manifest.get('dependencies')
    return (build.get('state')=='complete' and isinstance(old,dict) and bool(dependencies)
            and old!=dependencies)
