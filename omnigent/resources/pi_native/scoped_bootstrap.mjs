import fs from "node:fs";
import path from "node:path";
import {randomUUID} from "node:crypto";
import {createRequire} from "node:module";
import {fileURLToPath, pathToFileURL} from "node:url";
import {installNativeSupportBootstrap} from "./omnigent_pi_bootstrap.mjs";
const require = createRequire(import.meta.url);
const support = require("./native_support.cjs");
const registry = require("./permission_tool_registry.cjs");
const {connectFromHandoff} = require("./scoped_client.cjs");
const resources = path.dirname(fileURLToPath(import.meta.url));
const guard = path.join(resources, "omnigent_pi_child_extension.js");

/** Called by the trusted launcher before any resource loader exists. */
export async function installScopedSdkFromHandoff(sdk, locator, sdkIdentity) {
  return installScopedSdk(sdk, await connectFromHandoff(locator), sdkIdentity);
}

export function installScopedSdk(sdk, {connection, bridgeDir, statePath}, {sdkVersion, sdkEntry}) {
  if(sdkVersion!=="1.3.0" || !path.isAbsolute(sdkEntry || "")) {
    connection.close();
    throw new Error("Scoped Pi SDK identity unavailable");
  }
  const readState = () => JSON.parse(fs.readFileSync(statePath, "utf8"));
  const loaders = new WeakMap(), registrations = new Map();
  let rootLoader, apiPromise;
  const runtimeInputs = ["scoped_bootstrap.mjs", "scoped_client.cjs", "native_support.cjs",
    "omnigent_pi_bootstrap.mjs", "permission_tool_registry.cjs"].map(name => support.snapshot(path.join(resources, name)));
  runtimeInputs.push(...[sdkEntry, path.join(path.dirname(sdkEntry),"core/resource-loader.js"),
    path.join(path.dirname(sdkEntry),"core/agent-session.js")].map(file=>support.snapshot(file)));
  async function packageApi(session) {
    if (!apiPromise) {
      const tool = session.getAllTools().find(item => item.name === "subagent");
      if (!tool) return null;
      const entry = tool.sourceInfo?.path;
      if (!path.isAbsolute(entry || "")) throw new Error("Native package identity unavailable");
      apiPromise = (async () => {
        let directory = path.dirname(fs.realpathSync(entry));
        while (!fs.existsSync(path.join(directory, "package.json"))) {
          if (path.dirname(directory) === directory) throw new Error("Native package manifest unavailable");
          directory = path.dirname(directory);
        }
        const api = await support.loadPackageApi(directory, sdk);
        const manifest = JSON.parse(fs.readFileSync(path.join(directory, "package.json")));
        const exported = manifest.exports?.["./required-child-extensions"];
        const target = typeof exported === "string" ? exported : exported?.import || exported?.default;
        if (!target?.startsWith("./")) throw new Error("Native required-extension API unavailable");
        const file = support.snapshot(path.join(directory, target));
        if (path.relative(fs.realpathSync(directory), file.resolved_path).startsWith(".."))
          throw new Error("Native required-extension API escapes package");
        const required = await import(pathToFileURL(file.resolved_path).href);
        if (typeof required.registerRequiredChildExtensions !== "function")
          throw new Error("Native required-extension registration unavailable");
        return {api, required, inputs:[...api.inputs, file]};
      })();
    }
    return apiPromise;
  }
  const dispose = installNativeSupportBootstrap(sdk, {
    sdkVersion, getRootState:readState,
    prepareLoader(loader) {
      if (!rootLoader) rootLoader = loader;
      if (!loaders.has(loader)) loaders.set(loader, randomUUID());
      if (!Array.isArray(loader.additionalExtensionPaths)) throw new Error("Native SDK loader layout unavailable");
      if (!loader.additionalExtensionPaths.includes(guard)) loader.additionalExtensionPaths.push(guard);
      return {getState:readState, bridgeDir};
    },
    async describeSession(session, state) {
      const handle = randomUUID();
      const pending = await connection.bind({loader:loaders.get(session.resourceLoader), handle});
      if (pending.ready !== false || pending.scope?.origin !== (session.resourceLoader === rootLoader ? "parent" : "child"))
        throw new Error("Native installation origin unavailable");
      const published = registry.capture(session, pending.expected);
      published.loaded_extensions = state.sources.map(source => ({
        path:source.path, resolved_path:source.resolved_path, source_sha256:source.sha256,
      }));
      const binding = await connection.bind({loader:loaders.get(session.resourceLoader), handle, registry:published});
      if (binding.ready !== true) throw new Error("Native registry acknowledgement unavailable");
      const pkg = await packageApi(session);
      if (pkg) {
        const id = session.sessionManager.getSessionId();
        registrations.get(id)?.dispose();
        registrations.set(id, pkg.required.registerRequiredChildExtensions({sessionId:id,
          extensions:[{id:"omnigent.permission", path:guard}], requireForAllRunners:true}));
      }
      const client = connection.client(binding, handle, readState,
        (event, ctx) => support.classifyNativeCall(event, ctx, readState()));
      return {scope:binding.scope, api:pkg?.api, parentSession:session.resourceLoader === rootLoader ? session : undefined,
        bridgeDir, getState:readState, scopedClient:client,
        requiredIdentities:[support.snapshot(guard)],
        configInputs:[...runtimeInputs, ...(pkg?.inputs || []), support.snapshot(pending.expected.extension_path),
          support.snapshot(JSON.parse(fs.readFileSync(path.join(bridgeDir,"permission_expected.json"))).extension_path)],
        beforeClose:() => client.close()};
    },
  });
  return () => {
    dispose();
    connection.close();
    for (const registration of registrations.values()) registration.dispose();
  };
}
