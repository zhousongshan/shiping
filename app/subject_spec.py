"""Derive visible identity and material roles from actual user images."""
import json
from . import planner, store
from .production_schema import artifact, file_hash, fingerprint

SUBJECT_POLICY_VERSION = 'creative-identity-v1'
SUBJECT_INSTRUCTION = '''依据真实图片、创作模式和用户要求判断素材用途。
输出JSON：assets数组（每项path、role为identity/scene/style/other、visible_traits数组），replacement_rules数组，uncertainties数组，question（仅必要对应不明时填写，否则空字符串）。
create模式没有参考视频，文字描述要创作的新动作和场景，图片提供主体身份或场景、风格参考。用户上传单个清晰商品/玩偶/角色图，并要求其运动、跳舞或到新场景时，通常将图中主体判为identity；即使用户只说“户外跳舞，很可爱”，也应结合图片理解创作主体。
静态图片不需要已经呈现目标动作或场景；不能因为商品展示图里没有户外或跳舞，就判为无关other、不可生成或要求上传跳舞视频。保留真实可见身份特征，动作与新场景来自用户要求。用户明确只用作背景或风格时遵循该用途，多图主体对应确实不明时才追问。不要把所有图片强制当主体。
reference模式按用户意图识别完整主体替换；主体替换不是给原脸换衣服，只有用户要求替换时才适用。多角色映射遵循用户意见。不得猜不可见外观，无图时assets为空。'''

def analyze(job, root):
    inputs = [{'path': f'assets/{a}.jpg', 'sha256': file_hash(root/'assets'/f'{a}.jpg')} for a in job.get('images', [])]
    mode = 'reference' if job.get('reference') else 'create'
    source_version = fingerprint({'inputs': inputs, 'goal': job.get('effective_prompt', job['prompt']), 'feedback': job.get('feedback'), 'mode': mode, 'policy_version': SUBJECT_POLICY_VERSION})
    if job.get('subject_spec', {}).get('source_version') == source_version:
        return job['subject_spec']
    if not inputs:
        # No uploaded image means there is no image-role ambiguity to resolve.
        # Reference video understanding belongs to the next stage, not a request
        # that employees upload an optional image before they may continue.
        result=artifact(root,'subjects',{'assets':[],'replacement_rules':[],'uncertainties':[],
            'question':'','source_version':source_version,'inputs':[]})
        store.update(job['id'],subject_spec=result)
        return result
    content = [{'type': 'text', 'text': json.dumps({'goal': job.get('effective_prompt', job['prompt']), 'feedback': job.get('feedback'), 'mode': mode, 'inputs': inputs}, ensure_ascii=False)}]
    for item in inputs:
        content += [{'type': 'text', 'text': item['path']}, planner.image_content(root/item['path'])]
    result = planner.chat(SUBJECT_INSTRUCTION, content)
    assets = result.get('assets')
    if not isinstance(assets, list) or len(assets) != len(inputs) or {a.get('path') for a in assets} != {a['path'] for a in inputs}:
        raise ValueError('主体分析缺少素材对应')
    if any(a.get('role') not in ('identity','scene','style','other') or not isinstance(a.get('visible_traits'), list) for a in assets):
        raise ValueError('主体用途或可见特征无效')
    if result.get('question'):
        store.update(job['id'], status='needs_input', question=result['question'])
        return {'question': result['question']}
    result = artifact(root, 'subjects', {**result, 'source_version': source_version, 'inputs': inputs})
    store.update(job['id'], subject_spec=result)
    return result
