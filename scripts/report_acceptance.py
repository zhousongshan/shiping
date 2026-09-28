"""Read-only summary of live acceptance artifacts; no model/provider calls."""
import json
from pathlib import Path
from app import config,store,media

labels={'text':'仅文字','image':'仅图片','text_image':'文字＋图片','reference':'仅参考视频','text_reference':'文字＋参考视频','image_reference':'图片＋参考视频','all_inputs':'文字＋图片＋参考视频','long_reference':'16秒参考＋图片（跨生成单元）'}
rows=[]
for case in json.loads((config.DATA/'acceptance.json').read_text()):
    j=store.get(case['job_id'])
    row={**case,'label':labels[case['case']],**{k:j.get(k) for k in ['status','duration','error','reserved_generation_seconds','final_review','composition']},'builds':[{k:b.get(k) for k in ['build_id','run','output','state','generation_seconds','contains_generated']} for b in j.get('agent_builds',{}).values()]}
    if j['status']=='completed':
        row['video']=str(Path(j['video']).resolve());row['media_check']=media.verify(j['video'],j['duration'])
    rows.append(row)
(config.DATA/'results-detailed.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
passed=sum(r['status']=='completed' for r in rows)
report=['# Hypit 核心功能验收记录（2026-09-24）','',f'本轮真实任务完成 {passed}/{len(rows)}。这个数字代表下列固定素材样例的结果，不代表任意视频效果均已验证。','',
'## 用户确认的范围','',
'本轮先保证商品、玩偶、场景素材。公司多人稳定使用、按意见修改指定片段、真人出镜暂缓；自动检查及制作中的返修保留。员工通过网页使用，无需 Codex。','',
'## 真实生成结果','',
'| 输入 | 目标/实测时长 | 状态 | 成片 |','| --- | --- | --- | --- |']
for r in rows:
    duration=f"{r['duration']:g}秒 / {r['media_check']['duration']:g}秒" if r.get('media_check') else f"{r['duration']}秒 / 待完成"
    link=f"[查看视频]({r['video']})" if r.get('video') else '未交付'
    report.append(f"| {r['label']} | {duration} | {r['status']} | {link} |")
report+=['','说明：短参考为已有玩偶视频前4秒；16秒样本为已有12.096秒参考延长结尾画面，目的是验证跨单元制作。所有任务使用真实火山模型与 Hypit；部分样例经历返修及代码修复后的续跑，不能当成八次全新任务一次成功率。','',
'## 检查与修复','',
'- 25项本地回归检查通过，包含真实Hypit合成、七种API输入组合、裁剪缓存、片段重排、静音素材、时长和完成门槛。前端JavaScript语法检查通过。',
'- 浏览器实际播放与下载范围请求正常；轮询没有打断播放；片段修改入口默认关闭。',
'- 修复生成后偶发空回合停住、模型填错输出名无法领取成片、同名裁剪复用旧区间、重排覆盖输入、播放器显示旧版本等问题。',
'- 实测发现审片把悬浮误当成台面放置，也发现稀疏观察误报精确转角与拼接跳动。增加参考画面和时间码对照、逐项事实检查、拼接点密集帧及实际音轨检查；不通过不得完成。',
'- 完成检查绑定当前视频内容及当前需求/方案；只有生成来源可追踪、时长正确且审核通过的结果才能交付。','',
'## 画面复看范围','']
manual=config.DATA/'manual-review.json'
if manual.exists():
    for r in json.loads(manual.read_text()):report.append(f"- {labels[r['case']]}：{r['note']}（{r['scope']}）")
report+=['','## 实际边界','',
'上述验收证明已覆盖的制作路径和样例，不承诺任意复杂参考逐帧一致、所有细节绝对不变或生成模型永不失败。失败会返修，达到预算或无法确认时保留结果并暂停，不能伪装完成。','',
'当前仍是本机服务，媒体地址使用临时Tunnel。公司多人部署、固定媒体域名和公司环境稳定性本轮未验收；前端目前接收本地参考文件，不解析抖音/小红书分享链接。','',
f"完整回执及审核记录：[results-detailed.json]({(config.DATA/'results-detailed.json').resolve()})。真实验收脚本：[accept_core.py]({config.ROOT/'scripts/accept_core.py'})。"]
target=config.ROOT.parent/'docs'/'Hypit核心功能验收记录-20260924.md'
target.write_text('\n'.join(report)+'\n');print(target)
