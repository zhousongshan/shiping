import {
  createRuntimeEndpointAdapterFacet, runtimeConfigCredentialRef, runtimeConfigExact,
  runtimeConfigObject, runtimeConfigPositiveInteger, runtimeConfigString,
} from "@hypit/hypit/runtime-kit";
import {createVideoProvider, providerModule} from "./provider.js";

export default {
  format: "hypit.node-package@1",
  hostFacets: [createRuntimeEndpointAdapterFacet({
    use: providerModule.name,
    activate(context) {
      const config = runtimeConfigObject(context.config, "AutoDL Wan video service");
      runtimeConfigExact(config, ["baseUrl", "apiKey", "concurrency", "pollIntervalMs", "journalDirectory", "mediaBaseUrl", "referenceDirectory", "mediaSecretFile"], "AutoDL Wan video service");
      const baseUrl = runtimeConfigString(config.baseUrl, "AutoDL Wan baseUrl");
      const apiKey = runtimeConfigCredentialRef(config.apiKey, "AutoDL Wan apiKey");
      if (!baseUrl || !apiKey || !context.pool) throw new Error("AutoDL Wan requires baseUrl, apiKey and pool");
      return {endpoint: createVideoProvider({
        journalDirectory: runtimeConfigString(config.journalDirectory, "journalDirectory"),
        instance: context.instance, pool: context.pool, baseUrl, apiKey,
        mediaBaseUrl: config.mediaBaseUrl || undefined,
        referenceDirectory: config.referenceDirectory,
        mediaSecretFile: config.mediaSecretFile,
        concurrency: runtimeConfigPositiveInteger(config.concurrency, "concurrency") ?? 1,
        pollIntervalMs: runtimeConfigPositiveInteger(config.pollIntervalMs, "pollIntervalMs") ?? 5_000,
      })};
    },
  })],
};
