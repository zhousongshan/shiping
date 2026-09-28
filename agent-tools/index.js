// Deliberately JS-compatible TypeScript; Node loads the checked-in JS copy.
import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { defineTool } from '@deepseek-ai/dsh-tools';
export const name = 'hypit-video-tools';
export const inject = ['tools', 'attachments'];
export function apply(ctx) {
  ctx.effect(() => ctx.tools.guard(exec => exec.name === 'hypit_action' ? undefined : 'Only task-scoped Hypit tools are enabled'));
  ctx.on('agent/created', ({agent}) => { agent.ctx.effect(() => agent.ctx.tools.restrict({allow:['hypit_action']})); });
  ctx.effect(() => ctx.tools.register(defineTool({
    name:'hypit_action',
    description:'Operate this video project. First action=context. Preferred workflows: generate_unit({unit_id,prompt,images?,duration?}) automatically prepares planned references and validated generation; collect_review({build_id}) collects and reviews a completed unit; compose_build({}) composes all accepted units and starts rendering. End the turn on pending; the supervisor resumes. Other actions: understand_subjects, analyze_reference, review_unit, context, docs, list, read, write, observe, analyze_video, cut, frame, extract_audio, conform_duration, set_plan, generation_source, generate_subject, check, build, status, collect, compose, review, finish, ask. args is a JSON object. build already validates; do not add a duplicate check. Never invent build IDs or claim an unchecked video is complete.',
    parameters:{action:{type:'string',required:true},args:{type:'object',additionalProperties:true,required:true}},
    timeoutMs:900000,
    output:{schema:{type:'string'},render:(_args,value)=>{
      const result=JSON.parse(value);
      const content=[{type:'text',text:JSON.stringify(result.data)}];
      for(const image of result.images || [])content.push({type:'image',attachment:image});
      return content;
    }},
    async execute(args,exec){
      const raw=await new Promise((resolve,reject)=>{
        const child=spawn(process.env.VIDEO_AGENT_PYTHON,['-m','app.agent_tools',process.env.VIDEO_AGENT_JOB_ID,args.action],{
          cwd:process.env.VIDEO_AGENT_ROOT,env:process.env,stdio:['pipe','pipe','pipe'],signal:exec.signal,
        });
        let out='',err='';
        child.stdout.on('data',b=>{out+=b;if(out.length>2000000)child.kill();});
        child.stderr.on('data',b=>{err=(err+b).slice(-4000);});
        child.on('error',reject);
        child.on('close',code=>code===0?resolve(out):reject(new Error(err || out || 'Video tool failed')));
        child.stdin.end(JSON.stringify(args.args));
      });
      const data=JSON.parse(raw),images=[];
      for(const path of data.image_paths || []){
        images.push(await ctx.attachments.saveImage({data:await readFile(path),mediaType:'image/jpeg',name:path.split('/').pop()}));
      }
      delete data.image_paths;
      return JSON.stringify({data,images});
    }
  })));
  ctx.provide('companyVideoTools', true);
}
