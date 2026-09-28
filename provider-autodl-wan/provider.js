import {canonicalize, defineEndpointPackage, wakeAfter} from "@hypit/hypit/endpoint-kit";
import {compileWireRequest, generationTypes, sealGeneratedVideoSet, mappingSupportsRequest} from "@hypit/hypit/generation";
import {createHash, createHmac} from "node:crypto";
import {mkdir, readFile, writeFile, rename, unlink} from "node:fs/promises";
import {join} from "node:path";
import {wanRequest, wanTask} from "./wan_wire.js";
import {checkConnection, transportFacts} from "./transport.js";

export const providerModule = {name: "@local/provider-autodl-wan", version: "1"};
// Hypit 0.2.7 has no Wan authoring component. Its Seedance surface supplies the
// same text, frame and reference ports; only this endpoint speaks Wan on the wire.
const capability = {module: {name: "@hypit/seedance", version: "1"}, name: "seedance-2"};
const mapping = {capability, result: "video", routes: [{model: "wan3.0-video"}], fields: {
  prompt: {as: "value", field: "prompt"}, duration: {as: "value", field: "duration"},
  resolution: {as: "value", field: "resolution"}, aspectRatio: {as: "value", field: "ratio"},
  generateAudio: {as: "value", field: "audio"}, webSearch: {as: "value", field: "webSearch"},
  referenceImage: {as: "urlArray", field: "images"}, referenceVideo: {as: "urlArray", field: "videos"},
  referenceAudio: {as: "urlArray", field: "audios"},
  firstFrame: {as: "url", field: "first"}, lastFrame: {as: "url", field: "last"},
}};

export function createVideoProvider(o) {
  const base = o.baseUrl.replace(/\/$/, "");
  async function record(file, value) {
    await mkdir(o.journalDirectory, {recursive: true});
    const temp = file + "." + process.pid + ".tmp";
    await writeFile(temp, JSON.stringify(value));
    await rename(temp, file);
  }
  async function api(path, key, body) {
    const response = await fetch(base + path, {
      method: body ? "POST" : "GET",
      headers: {Authorization: "Bearer " + key, "Content-Type": "application/json",
        ...(body ? {"X-DashScope-Async": "enable"} : {})},
      ...(body ? {body: JSON.stringify(body)} : {}), signal: AbortSignal.timeout(180000),
    });
    let data;
    try { data = await response.json(); }
    catch {
      const error = new Error("AutoDL Wan HTTP " + response.status + " returned a non-JSON response");
      error.httpStatus = response.status;
      throw error;
    }
    if (!response.ok || data?.code && !data?.output?.task_id) {
      const detail = String(data?.error?.message ?? data?.message ?? data?.code ?? "request_failed")
        .replaceAll(key, "[redacted]").replace(/https?:\/\/\S+/g, "[url]").slice(0, 500);
      const error = new Error("AutoDL Wan HTTP " + response.status + " " + detail);
      error.httpStatus = response.status;
      throw error;
    }
    return data;
  }
  const support = request => mappingSupportsRequest(mapping, request.constraints)
    ? {status: "supported"} : {status: "unsupported", reason: "Unsupported Wan request"};
  return defineEndpointPackage({
    module: providerModule, facet: "videos", instance: o.instance, pool: o.pool,
    credentials: {apiKey: o.apiKey}, credentialInputs: {apiKey: {label: "AutoDL API key"}},
    defaultConcurrency: 1,
    actionLimits: {submit: {concurrency: 1}, poll: {concurrency: 3}, collect: {concurrency: 1}},
    capabilities: [{capability, returns: generationTypes.videoSet, lifecycle: "asynchronous", supports: support, endpoint: {
      async start(c) {
        const compiled = await compileWireRequest(mapping, c.need.constraints, async artifact => {
          const bytes = await c.resources.get(artifact.resource);
          if (!bytes) throw Error("Reference missing");
          if (artifact.mediaType.startsWith("video/") || artifact.mediaType.startsWith("audio/")) {
            if (!o.mediaBaseUrl || !o.referenceDirectory || !o.mediaSecretFile)
              throw Error("MEDIA_GATEWAY_NOT_CONFIGURED: reference video/audio needs a model-accessible signed media URL");
            const ext = artifact.mediaType.startsWith("video/") ? "mp4" : artifact.mediaType.includes("wav") ? "wav" : "mp3";
            const name = createHash("sha256").update(bytes).digest("hex") + "." + ext;
            await mkdir(o.referenceDirectory, {recursive: true});
            await writeFile(join(o.referenceDirectory, name), bytes);
            const expires = Math.ceil(Date.now() / 86400000) * 86400 + 86400;
            const sig = createHmac("sha256", await readFile(o.mediaSecretFile)).update(name + ":" + expires).digest("hex");
            return o.mediaBaseUrl + "/media/" + name + "?expires=" + expires + "&signature_value=" + sig;
          }
          return "data:" + artifact.mediaType + ";base64," + Buffer.from(bytes).toString("base64");
        });
        const request = wanRequest(compiled.input);
        if (!o.journalDirectory) throw Error("Missing submission journal directory");
        await c.reportProgress?.({phase: "Submitting AutoDL Wan 3.0 video"});
        const identity = JSON.stringify(request).replace(/\?expires=\d+&signature_value=[a-f0-9]+/g, "");
        const file = join(o.journalDirectory, createHash("sha256").update(identity).digest("hex") + ".json");
        let existing;
        try { existing = JSON.parse(await readFile(file, "utf8")); }
        catch (e) { if (e.code !== "ENOENT") throw e; }
        if (existing && !existing.id) throw Error("SUBMISSION_UNCERTAIN: prior Wan submission has no receipt");
        let id = existing?.id;
        if (!id) {
          await checkConnection(base);
          await record(file, {state: "intent", time: new Date().toISOString()});
          let made;
          try { made = await api("/api/v1/services/aigc/video-generation/video-synthesis", c.credentials.apiKey.secret, request); }
          catch (e) {
            if ([400, 401, 403, 404, 422].includes(e.httpStatus)) { await unlink(file); throw e; }
            await record(file, {state: "uncertain", time: new Date().toISOString(),
              httpStatus: e.httpStatus ?? null, error: String(e.message).slice(0, 600), transport:transportFacts(e)});
            throw Error("SUBMISSION_UNCERTAIN: " + String(e.message).slice(0, 600));
          }
          id = wanTask(made).id;
          if (!id) throw Error("SUBMISSION_UNCERTAIN: Wan response has no task ID");
          await record(file, {state: "submitted", id, time: new Date().toISOString()});
        }
        const handle = {id};
        await c.checkpoint?.({handle, receipt: handle});
        return {...wakeAfter(handle, 15000), receipt: handle};
      },
      async poll(c) {
        let task;
        try {
          task = wanTask(await api("/api/v1/tasks/" + encodeURIComponent(c.handle.id), c.credentials.apiKey.secret));
        } catch (error) {
          // A failed read does not invalidate the accepted task receipt. Keep
          // polling the same task instead of failing the entire paid Build.
          const detail = String(error?.message ?? error);
          if (/fetch failed|ECONN|ETIMEDOUT|timeout|timed out|socket|DNS|HTTP (?:429|5\d\d)/i.test(detail))
            return wakeAfter(c.handle, 15000, Date.now(), {phase: "poll_retry"});
          throw error;
        }
        const status = String(task.status ?? "").toUpperCase();
        if (["PENDING", "RUNNING", "QUEUED"].includes(status))
          return wakeAfter(c.handle, 15000, Date.now(), {phase: status.toLowerCase()});
        if (status !== "SUCCEEDED") return {status: "failed", receipt: {id: c.handle.id},
          failure: {code: task.code ?? "WAN_FAILED", message: String(task.message ?? status)}};
        if (!task.url) throw Error("No video URL in successful Wan task");
        return {status: "ready", handle: {id: c.handle.id, url: task.url},
          receipt: {id: c.handle.id, usage: task.usage}};
      },
      async collect(c) {
        const response = await fetch(c.handle.url, {signal: AbortSignal.timeout(180000)});
        if (!response.ok) throw Error("Wan video download failed " + response.status);
        const artifact = await c.resources.put(new Uint8Array(await response.arrayBuffer()), "video/mp4");
        return {status: "completed", result: {value: {kind: "inline", value: canonicalize(sealGeneratedVideoSet({videos: [artifact]}))}}};
      },
    }}],
  });
}
