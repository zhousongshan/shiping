"""Resolve reference identities before planning any subject replacement."""
import json
from . import planner,store
from .production_schema import artifact,fingerprint

def required(job):
    if job.get("workflow_version",0)>=4 and not job.get("creative_brief",{}).get("replacement_required"):
        return False
    return bool(job.get('workflow_version',0)>=3 and job.get('reference') and
                any(a.get('role')=='identity' for a in job.get('subject_spec',{}).get('assets',[])))

def key(job):
    return fingerprint([job.get('prompt'),job.get('feedback'),job.get('subject_spec',{}).get('version'),
                        job.get('reference_analysis',{}).get('version'),'subject-mapping-v1'])

def current(job):
    value=job.get('subject_mapping') or {}
    return value if value.get('source_version')==key(job) and value.get('status')=='resolved' else None

def resolve(job,root):
    if not required(job):return None
    if current(job):return current(job)
    reference=job.get('reference_analysis') or {}
    if reference.get('status')!='verified':raise ValueError('先完成参考视频分析，再确定替换对象')
    user_text='\n'.join(str(job.get(k) or '') for k in ('prompt','feedback')).strip()
    result=planner.chat('''你只负责确定图片主体替换参考视频中的谁，不制作视频。
根据已经观察的参考事件，把同一角色的别名合并，列出主要角色；背景、星星、礼盒、灯、彩虹等道具不算角色。
输出JSON：subjects（主要角色名称字符串数组），selection（unique/explicit/ambiguous），targets（被替换的角色名称数组），user_quote（用户原文中明确指定替换范围的连续原句；没有则空字符串）。
只有一个主要角色可用unique；多角色且用户明确指定一个或全部时用explicit。多角色而用户未指定替换谁必须ambiguous，不能凭系统推断的“替换原主体”把所有角色都替换。
targets只能来自subjects。不要把观察事实中的角色名称当作用户的替换指令。''',
        [{'type':'text','text':json.dumps({'user_text':user_text,'images':job['subject_spec']['assets'],
            'events':[{k:e.get(k) for k in ('second','description','subjects')} for e in reference.get('events',[])]},ensure_ascii=False)}])
    subjects=result.get('subjects');targets=result.get('targets',[])
    if not isinstance(subjects,list) or not subjects or any(not isinstance(s,str) or not s.strip() for s in subjects):
        raise ValueError('参考角色识别缺少可核对的角色名称')
    if not isinstance(targets,list) or any(t not in subjects for t in targets):
        raise ValueError('替换目标不在实际参考角色列表中')
    quote=result.get('user_quote','')
    resolved=(len(subjects)==1 and result.get('selection')=='unique' and targets==subjects) or (
        result.get('selection')=='explicit' and bool(targets) and isinstance(quote,str) and bool(quote.strip()) and quote in user_text)
    if len(subjects)>1 and not user_text:resolved=False
    introduction='参考视频中有'+'、'.join(subjects)+'。' if len(subjects)<=4 else '参考视频里出现了多个角色。'
    question='' if resolved else introduction+'你希望用上传图片中的主体替换哪一个？请描述其外观或出现位置；如果要全部替换，也请明确说明。'
    record=artifact(root,'subject-mapping',{**result,'source_version':key(job),
        'status':'resolved' if resolved else 'needs_input','question':question,
        'preserve_subjects':[s for s in subjects if s not in targets] if resolved else subjects})
    store.update(job['id'],subject_mapping=record)
    return record
