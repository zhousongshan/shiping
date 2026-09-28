"""Deterministic guardrails around semantic quality judgments."""
import copy
import re

REVIEW_VERSION='evidence-v4-audio-levels'


def reconcile(result, observations):
    result=copy.deepcopy(result)
    audio=[o.get('audio',{}) for o in observations]
    incomplete=any(a.get('present',a.get('reason')!='no audio track') and not a.get('checked') for a in audio)
    contradictions=[]
    if any(a.get('present') for a in audio):
        for check in result.get('checks',[]):
            if re.search(r'无音轨|没有音轨|未检测到音轨|无音频流|没有音频流|no audio track',check.get('evidence',''),re.I):
                contradictions.append(copy.deepcopy(check))
                check.update(status='warn',evidence='文件实际含有音轨；原审片声称无音轨，与技术检测矛盾，需要复核声音内容。')
    # An invalid observation is an inspection failure, never a paid repair trigger.
    if incomplete or contradictions:
        result.update(verdict='warn',review_state='inspection_incomplete',
                      summary='声音检查未完成或证据矛盾；视频已保存，需复核，不自动重新生成。')
        result['issues']=[*result.get('issues',[]),result['summary']]
        result['contradictory_checks']=contradictions
    else:
        result['review_state']='content_failed' if result.get('verdict')=='fail' else 'insufficient_evidence' if result.get('verdict')=='warn' else 'passed'
    result['review_version']=REVIEW_VERSION
    return result
