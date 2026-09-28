import {canonicalize,defineEndpointPackage,wakeAfter} from "@hypit/hypit/endpoint-kit";
import {compileWireRequest,generationTypes,sealGeneratedVideoSet,mappingSupportsRequest} from "@hypit/hypit/generation";
import {createHash,createHmac} from "node:crypto";
import {mkdir,readFile,writeFile,rename,unlink} from "node:fs/promises";
import {join} from "node:path";
export const providerModule={name:"@local/provider-ark",version:"1"};
const capability={module:{name:"@hypit/seedance",version:"1"},name:"seedance-2"};
const mapping={capability,result:"video",routes:[{model:"doubao-seedance-2-0-260128"}],fields:{
 prompt:{as:"value",field:"prompt"},duration:{as:"value",field:"duration"},resolution:{as:"value",field:"resolution"},aspectRatio:{as:"value",field:"ratio"},generateAudio:{as:"value",field:"generate_audio"},webSearch:{as:"value",field:"webSearch"},
 referenceImage:{as:"urlArray",field:"images"},referenceVideo:{as:"urlArray",field:"videos"},referenceAudio:{as:"urlArray",field:"audios"},
 firstFrame:{as:"url",field:"first"},lastFrame:{as:"url",field:"last"}
}};
export function createVideoProvider(o){
 const base=o.baseUrl.replace(/\/$/,"");
 async function record(file,value){await mkdir(o.journalDirectory,{recursive:true});const temp=file+"."+process.pid+".tmp";await writeFile(temp,JSON.stringify(value));await rename(temp,file);}
 const support=r=>mappingSupportsRequest(mapping,r.constraints)?{status:"supported"}:{status:"unsupported",reason:"Unsupported Ark request"};
 async function api(path,key,body){
  const r=await fetch(base+path,{method:body?"POST":"GET",headers:{Authorization:"Bearer "+key,"Content-Type":"application/json"},...(body?{body:JSON.stringify(body)}:{}),signal:AbortSignal.timeout(180000)});
  const d=await r.json();
  if(!r.ok){const e=new Error("Ark HTTP "+r.status+" "+JSON.stringify(d.error??d).replace(/https?:\/\/\S+/g,"[url]"));e.httpStatus=r.status;throw e;}
  return d;
 }
 return defineEndpointPackage({module:providerModule,facet:"videos",instance:o.instance,pool:o.pool,credentials:{apiKey:o.apiKey},credentialInputs:{apiKey:{label:"Ark API key"}},defaultConcurrency:1,actionLimits:{submit:{concurrency:1},poll:{concurrency:3},collect:{concurrency:1}},capabilities:[{capability,returns:generationTypes.videoSet,lifecycle:"asynchronous",supports:support,endpoint:{
  async start(c){
   const compiled=await compileWireRequest(mapping,c.need.constraints,async a=>{
    const b=await c.resources.get(a.resource);if(!b)throw Error("Reference missing");
    if(a.mediaType.startsWith("video/") || a.mediaType.startsWith("audio/")){
     if(!o.mediaBaseUrl || !o.referenceDirectory || !o.mediaSecretFile)throw Error("MEDIA_GATEWAY_NOT_CONFIGURED: reference video/audio needs a model-accessible signed media URL");
     const ext=a.mediaType.startsWith("video/")?"mp4":a.mediaType.includes("wav")?"wav":"mp3";
     const name=createHash("sha256").update(b).digest("hex")+"."+ext;
     await mkdir(o.referenceDirectory,{recursive:true});
     await writeFile(join(o.referenceDirectory,name),b);
     // Use a stable time bucket so recovery reuses the same submission journal.
     const expires=Math.ceil(Date.now()/1000/86400)*86400+86400;
     const sig=createHmac("sha256",await readFile(o.mediaSecretFile)).update(name+":"+expires).digest("hex");
     return o.mediaBaseUrl+"/media/"+name+"?expires="+expires+"&signature_value="+sig;
    }
    return "data:"+a.mediaType+";base64,"+Buffer.from(b).toString("base64");
   });
   const w={...compiled.input,model:compiled.model};
   const content=[{type:"text",text:w.prompt}];
   for(const [field,type,role] of [["images","image_url","reference_image"],["videos","video_url","reference_video"],["audios","audio_url","reference_audio"]])
    for(const url of w[field]??[])content.push({type,[type]:{url},role});
   for(const [field,role] of [["first","first_frame"],["last","last_frame"]])if(w[field])content.push({type:"image_url",image_url:{url:w[field]},role});
   if(w.webSearch)throw Error("Web search not supported by this adapter");
   await c.reportProgress?.({phase:"Submitting official Ark Seedance 2.0"});
   const request={model:w.model,content,duration:w.duration,resolution:w.resolution,ratio:w.ratio,generate_audio:w.generate_audio,watermark:false};
   if(!o.journalDirectory)throw Error("Missing submission journal directory");
   // Expiring URL signatures must not change the identity of a paid request.
   const identity=JSON.stringify(request).replace(/\?expires=\d+&signature_value=[a-f0-9]+/g,"");
   const digest=createHash("sha256").update(identity).digest("hex");
   const file=join(o.journalDirectory,digest+".json");
   let existing;
   try{existing=JSON.parse(await readFile(file,"utf8"));}catch(e){if(e.code!=="ENOENT")throw e;}
   if(existing&&!existing.id)throw Error("SUBMISSION_UNCERTAIN: prior submission has no receipt; reconcile before another paid request");
   const d=existing??await (async()=>{await record(file,{state:"intent",time:new Date().toISOString()});let made;try{made=await api("/contents/generations/tasks",c.credentials.apiKey.secret,request);}catch(e){if([400,401,403,404,422].includes(e.httpStatus))await unlink(file);throw e;}if(!made.id)throw Error("SUBMISSION_UNCERTAIN: missing task ID");await record(file,{state:"submitted",id:made.id,time:new Date().toISOString()});return made;})();
   const handle={id:d.id};await c.checkpoint?.({handle,receipt:handle});
   return {...wakeAfter(handle,15000),receipt:handle};
  },
  async poll(c){
   const d=await api("/contents/generations/tasks/"+encodeURIComponent(c.handle.id),c.credentials.apiKey.secret);
   if(["queued","running"].includes(d.status))return wakeAfter(c.handle,15000,Date.now(),{phase:d.status});
   if(d.status!=="succeeded")return {status:"failed",receipt:{id:c.handle.id},failure:{code:d.error?.code??"ARK_FAILED",message:d.error?.message??d.status}};
   if(!d.content?.video_url)throw Error("No video in successful task");
   return {status:"ready",handle:{id:c.handle.id,url:d.content.video_url},receipt:{id:c.handle.id,usage:d.usage}};
  },
  async collect(c){
   const r=await fetch(c.handle.url,{signal:AbortSignal.timeout(180000)});
   if(!r.ok)throw Error("Video download failed "+r.status);
   const a=await c.resources.put(new Uint8Array(await r.arrayBuffer()),"video/mp4");
   return {status:"completed",result:{value:{kind:"inline",value:canonicalize(sealGeneratedVideoSet({videos:[a]}))}}};
  }
 }}]});
}
