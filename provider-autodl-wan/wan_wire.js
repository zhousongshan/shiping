export function wanRequest(w) {
  if (w.webSearch) throw Error("Wan 3.0 does not support web search in this adapter");
  const references = [...(w.images ?? []).map(url => ({type: "reference_image", url})),
    ...(w.videos ?? []).map(url => ({type: "reference_video", url})),
    ...(w.audios ?? []).map(url => ({type: "reference_audio", url}))];
  const frames = [...(w.first ? [{type: "first_frame", url: w.first}] : []),
    ...(w.last ? [{type: "last_frame", url: w.last}] : [])];
  if (frames.length && references.length) throw Error("Wan frame and reference modes cannot be combined");
  if (w.last && !w.first) throw Error("Wan last frame requires first frame");
  const media = frames.length ? frames : references;
  return {
    model: "wan3.0-video",
    input: {prompt: w.prompt, ...(media.length ? {media} : {})},
    parameters: {
      duration: w.duration,
      resolution: String(w.resolution ?? "720p").toUpperCase(),
      ratio: w.ratio ?? "adaptive",
      audio: w.audio ?? true,
      prompt_extend: true,
      watermark: false,
    },
  };
}

export function wanTask(response) {
  const output = response?.output ?? {};
  return {id: output.task_id, status: output.task_status, url: output.video_url,
    message: output.message ?? response?.message, code: output.code ?? response?.code,
    usage: response?.usage};
}
