const $=id=>document.getElementById(id);
let active=null;
let features={revisions:false};
let lastPlanKey=null;
let pendingSubmission=null;
let identity=null;
let pendingQuestionKey=null;
const labels={queued:'排队中',planning:'准备制作',analyzing:'理解素材',preparing_assets:'准备素材',generating:'生成片段',checking:'检查视频',rendering:'合成视频',agent_running:'正在制作',waiting_build:'等待视频生成',completed:'已完成',failed:'处理失败',needs_attention:'处理已暂停',needs_configuration:'等待服务配置',needs_review:'需要复核',needs_revision:'需要修改',needs_input:'需要补充说明'};
async function api(url,opts){const r=await fetch(url,opts);let d;try{d=await r.json()}catch{throw Error('服务返回了无法识别的结果')}if(r.status===401)$('login').hidden=false;if(!r.ok){const detail=Array.isArray(d.detail)?d.detail.map(x=>x.msg).join('；'):d.detail;throw Error(detail||'请求失败')}return d}
async function upload(file,kind){return api('/api/assets?kind='+kind+'&name='+encodeURIComponent(file.name),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file})}
function node(tag,text,cls){const el=document.createElement(tag);el.textContent=text;if(cls)el.className=cls;return el}
function title(j){return j.prompt||j.inferred_intent||'素材视频任务'}
function show(job){
 active=job.id;$('current').hidden=false;$('status').textContent=job.status_label||labels[job.status]||job.status;
 const questionKey=job.status==='needs_input'&&job.question?`${job.id}:${job.question}`:null;
 if(questionKey!==pendingQuestionKey){pendingQuestionKey=questionKey;$('answer-text').value='';$('answer-message').textContent=''}
 $('question-card').hidden=!questionKey;
 if(questionKey){$('question-text').textContent=job.question;$('message').textContent='当前任务需要补充说明，请回答页面上方的问题。'}
 else if($('message').textContent==='当前任务需要补充说明，请回答页面上方的问题。')$('message').textContent='';
 const timing=job.duration_mode==='fixed'?'用户指定':job.duration_mode==='reference'?'跟随参考':'系统规划';
 $('jobtext').textContent=title(job)+(job.duration?' · '+Number(job.duration.toFixed(2))+' 秒（'+timing+'）':'');$('error').textContent=job.error||'';
 $('events').replaceChildren(...(job.events||[]).slice(-4).map(e=>node('p',new Date(e.time*1000).toLocaleTimeString('zh-CN',{hour12:false})+' · '+e.message)));
 const seconds=s=>s<60?`${Math.round(s)} 秒`:`${Math.floor(s/60)} 分 ${Math.round(s%60)} 秒`;
 const names={understand_subjects:'识别素材',analyze_reference:'分析参考视频',plan_content:'编写制作方案',set_plan:'保存方案',generate_unit:'准备并提交生成',collect_review:'下载并检查片段',collect:'下载视频',review_unit:'检查片段',compose_build:'准备合成',review:'检查成片',finish:'交付'};
 $('timings').replaceChildren(...Object.entries(job.stage_seconds||{}).map(([s,n])=>node('p',`${labels[s]||s}：${seconds(n)}`)),node('p','步骤执行耗时（包含重试，可能与上方阶段重叠）：'),...Object.entries(job.tool_timings||{}).filter(([a])=>names[a]).map(([a,t])=>node('p',`${names[a]}：${seconds(t.seconds)} · ${t.calls} 次`)));
 if(!job.timing_complete)$('timings').prepend(node('p','历史任务的阶段时间记录不完整，已有步骤耗时仍保留。'));
 const planKey=JSON.stringify([job.id,job.status,job.plan,job.final_review,job.reviews,job.question,job.needs_reconciliation,job.result_version,job.saved_clips,job.candidate_delivery,features]);
 if(planKey!==lastPlanKey){lastPlanKey=planKey;
 const area=$('plan');area.replaceChildren();
 if(job.plan){area.append(node('h3',job.plan.theme||'制作方案'),node('p',job.plan.story||''));(job.plan.segments||[]).forEach((s,i)=>{
  const card=node('div','','segment');card.append(node('strong',`${i+1}. ${s.title||'片段'} · ${s.duration} 秒`),node('p',s.description||''));
  if(job.engine!=='hypit-agent-v5'&&['completed','needs_review','needs_revision'].includes(job.status)){const v=document.createElement('video');v.controls=true;v.preload='none';v.src=`/api/jobs/${job.id}/clips/${i}?v=${encodeURIComponent(job.result_version||'')}`;card.append(v)}
  if(features.revisions&&['completed','needs_review','needs_revision','failed','needs_attention'].includes(job.status)){const b=node('button','修改这一段');b.onclick=async()=>{const instruction=window.prompt('希望怎样修改这一段？');if(!instruction)return;try{show(await api(`/api/jobs/${job.id}/revisions`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:i,instruction})}))}catch(e){$('message').textContent=e.message}};card.append(b)}
  const review=job.reviews?.[String(i)];if(review)card.append(node('p','检查：'+review.summary));area.append(card);
 })}
 if(job.candidate_delivery)area.append(node('p','已有片段已合成，可预览下载；此版本未标记内容检查通过。'));
 if((job.saved_clips||[]).length){
 const box=node('div');box.append(node('h3','已生成的视频'),node('p','每个片段选择一个版本；合成按制作方案顺序进行。'));const selected=[];
 const groups=Object.groupBy(job.saved_clips,c=>c.unit_id||c.build_id);
 const quality={pass:'检查通过',fail:'检查未通过',warn:'需要复核',unchecked:'尚未完成检查',historical:'历史检查，待按新版复核'};
 for(const [unit,clips] of Object.entries(groups))for(const [index,clip] of clips.entries()){
  const card=node('div','','segment'),label=node('label'),input=document.createElement('input');input.type='radio';input.name=`version-${job.id}-${unit}`;input.checked=index===clips.length-1;selected.push([input,clip.build_id]);
  label.append(input,document.createTextNode(clip.title+(clip.duration?` · ${clip.duration.toFixed(2)} 秒`:'')));card.append(label,node('p',quality[clip.quality_status]||'尚未完成检查'));
  if(clip.has_audio!==null)card.append(node('p',clip.has_audio?'文件含音轨；声音内容是否合格以检查结果为准。':'文件没有音轨。'));
  if(clip.review_summary)card.append(node('p',clip.review_summary));
  const v=document.createElement('video');v.controls=true;v.playsInline=true;v.preload='none';v.src=clip.preview_url+'?v='+clip.file_version;card.append(v);
  const download=node('a','下载这个版本');download.href=clip.preview_url+'?download=true';download.download='hypit-clip.mp4';card.append(download);box.append(card);
 }
 if(!['queued','planning','agent_running','waiting_build','generating','rendering'].includes(job.status)){
 const b=node('button','合成所选版本（不重新生成）');b.onclick=async()=>{b.disabled=true;try{show(await api(`/api/jobs/${job.id}/compose-candidate`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({build_ids:selected.filter(([i])=>i.checked).map(([,id])=>id)})}))}catch(e){$('message').textContent=e.message}finally{b.disabled=false}};box.append(b)}area.append(box)}
 if(job.final_review)area.append(node('p','成片检查：'+job.final_review.summary));
 if(['failed','needs_configuration','needs_attention','needs_review','needs_revision'].includes(job.status)&&!job.needs_reconciliation&&(job.engine==='hypit-agent-v5'||!['needs_review','needs_revision'].includes(job.status))){const b=node('button','继续处理');b.onclick=async()=>{try{show(await api(`/api/jobs/${job.id}/retry`,{method:'POST'}))}catch(e){$('message').textContent=e.message}};area.append(b)}
 if(job.needs_reconciliation){const b=node('button','核对提交状态');b.onclick=async()=>{try{const r=await api(`/api/jobs/${job.id}/reconcile`,{method:'POST'});if(r.job)show(r.job);else $('message').textContent=r.message}catch(e){$('message').textContent=e.message}};area.append(b)}
 }
 const done=job.status==='completed',preview=done||job.candidate_available;
 $('result').hidden=!preview;$('download').hidden=!preview;
 if(preview){const url=`/api/jobs/${job.id}/video?${done?'':'candidate=true&'}v=${encodeURIComponent(job.result_version||'')}`;if($('result').getAttribute('src')!==url)$('result').src=url;$('download').href=url;$('download').download='hypit-video.mp4';$('download').textContent=done?'下载成片':'下载待检查版本'}
}
async function refresh(){try{if(!identity){identity=await api('/api/me');$('account').hidden=!identity.authentication;$('account-name').textContent=identity.user;$('admin-open').hidden=!identity.admin}const all=await api('/api/jobs');$('jobs').replaceChildren(...all.slice(0,20).map(j=>{const b=node('button',title(j).slice(0,60)+' · '+(labels[j.status]||j.status));b.onclick=()=>select(j.id);return b}));if(active)show(await api('/api/jobs/'+active));else if(all.length)show(all[0])}catch(e){$('message').textContent=e.message}}
async function select(id){active=id;show(await api('/api/jobs/'+id));window.scrollTo({top:0,behavior:'smooth'})}
$('form').onsubmit=async e=>{e.preventDefault();const button=$('submit');button.disabled=true;try{
 if(!pendingSubmission){const health=await api('/api/health');if(health.generation?.available===false)throw Error(health.generation.reason)}
 const files=[...$('images').files],ref=$('reference').files[0],prompt=$('prompt').value.trim();
 if(!prompt&&!files.length&&!ref)throw Error('请至少提供文字、图片或参考视频中的一项');
 if(files.length>6)throw Error('最多上传6张图片');$('message').textContent='正在上传素材…';
 if(!pendingSubmission){
  const images=[];for(const f of files)images.push((await upload(f,'image')).id);
  const reference=ref?(await upload(ref,'video')).id:null;
  pendingSubmission={key:crypto.randomUUID(),body:{prompt,timing_mode:$('timing-mode').value,duration:$('duration').value?Number($('duration').value):null,ratio:$('ratio').value||null,images,reference}};
 }
 const j=await api('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':pendingSubmission.key},body:JSON.stringify(pendingSubmission.body)});
 pendingSubmission=null;
 $('message').textContent='任务已提交，请查看当前任务状态。';show(j);await refresh();
}catch(err){$('message').textContent=err.message}finally{button.disabled=false}};
// A network retry preserves the uploaded asset IDs and submission key. Editing
// the form explicitly starts a new request instead of changing an existing key.
$('form').addEventListener('input',()=>{pendingSubmission=null});
$('form').addEventListener('change',()=>{pendingSubmission=null});
$('answer-form').onsubmit=async e=>{e.preventDefault();const jobId=active,instruction=$('answer-text').value.trim(),button=$('answer-submit');if(!jobId||!pendingQuestionKey||!instruction)return;button.disabled=true;$('answer-message').textContent='正在提交…';try{const job=await api(`/api/jobs/${jobId}/answer`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({instruction})});show(job);$('message').textContent='补充说明已提交，任务将继续处理。';await refresh()}catch(err){$('answer-message').textContent=err.message}finally{button.disabled=false}};
$('login-form').onsubmit=async e=>{e.preventDefault();try{await api('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('username').value,token:$('token').value})});$('token').value='';$('login').hidden=true;identity=null;active=null;pendingSubmission=null;lastPlanKey=null;$('current').hidden=true;$('admin-panel').hidden=true;await refresh()}catch(err){$('login-message').textContent=err.message}};
$('logout').onclick=async()=>{try{await api('/api/logout',{method:'POST'});identity=null;active=null;pendingSubmission=null;lastPlanKey=null;$('account').hidden=true;$('current').hidden=true;$('admin-panel').hidden=true;$('jobs').replaceChildren();$('result').removeAttribute('src');$('login').hidden=false}catch(e){$('message').textContent=e.message}};
$('admin-open').onclick=async()=>{try{const s=await api('/api/admin/status');$('admin-panel').hidden=false;$('admin-state').replaceChildren(node('p',`执行服务：${s.worker_alive?'在线':'未检测到独立执行服务'}；参考素材地址：${s.reference_configured?'已配置（仍需验证读取）':'未配置'}`),...s.attention.map(j=>node('p',`${j.owner} · ${j.id} · ${labels[j.status]||j.status}：${j.error||'需要检查'}`)))}catch(e){$('message').textContent=e.message}};
api('/api/health').then(h=>{features=h.features||features;if(h.generation?.available===false)$('message').textContent=h.generation.reason;if(!h.reference_ready){const note=node('small','参考视频制作暂待素材通道配置；上传的素材会保存，配置完成后可继续。');$('reference').parentElement.append(note)}refresh()}).catch(()=>refresh());setInterval(refresh,5000);
