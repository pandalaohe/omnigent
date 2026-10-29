import {createRequire} from "node:module";
const require = createRequire(import.meta.url);
const nativeSupport = require("./native_support.cjs");

/** Install before SDK loaders exist; the launcher owns profiles and scope transport. */
export function installNativeSupportBootstrap(sdk, {prepareLoader, describeSession, evaluate, getRootState, sdkVersion}) {
  const loader = sdk?.DefaultResourceLoader?.prototype;
  const session = sdk?.AgentSession?.prototype;
  const required = ["reload", "loadExtensionPaths", "resolveExtensionLoadPath", "loadExtensionFactories", "getExtensions", "loadCurrentExtensionSet"];
  if (sdkVersion !== "1.3.0" || !loader || !session || required.some(name => typeof loader[name] !== "function") || typeof session.bindExtensions !== "function" ||
      typeof prepareLoader !== "function" || typeof describeSession !== "function") {
    if (typeof getRootState === "function" && getRootState()?.tier === "full") return () => {};
    throw new Error("Unsupported native SDK bootstrap contract");
  }
  const store = globalThis[Symbol.for("omnigent.pi.native-support.v2")];
  const original = Object.fromEntries(required.map(name => [name, loader[name]]));
  const bind = session.bindExtensions;
  const installation = {active:true};
  const wraps = {};
  wraps.loadCurrentExtensionSet = function (...args) {
    const profile = prepareLoader(this);
    if (profile?.getState().tier === "full") return original.loadCurrentExtensionSet.apply(this, args);
    throw new Error("Native pre-trust extension loading is not admitted");
  };
  wraps.loadExtensionPaths = function (paths, runtime) {
    const state = store.loaders.get(this);
    if (!state) throw new Error("Independent native bootstrap is missing before loading");
    if (state.profile.getState().tier === "full") return original.loadExtensionPaths.call(this, paths, runtime);
    const check=nativeSupport.createInputGuard(state.profile.getState().workspace,state.profile.bridgeDir);
    for (const input of paths) {
      const resolved = this.resolveExtensionLoadPath(input);
      if (resolved.startsWith("builtin:")) continue;
      if (state.profile.checkInput) state.profile.checkInput(resolved);
      state.sources.push(check(resolved));
    }
    return original.loadExtensionPaths.call(this, paths, runtime);
  };
  wraps.reload = async function (...args) {
    const previous = store.loaders.get(this);
    if (previous?.manager) nativeSupport.closeNativeSession(previous.manager);
    const profile = prepareLoader(this);
    if (!profile || typeof profile.getState !== "function")
      throw new Error("Native loader profile is unavailable");
    const state = {profile, sources:[], manager:null, finalExtension:null, finalHook:null};
    store.loaders.set(this, state);
    await original.reload.apply(this, args);
    const result = this.getExtensions();
    if (!Array.isArray(result.extensions) || !Array.isArray(result.errors)) throw new Error("Malformed native SDK load result");
    // Let the real SDK/package deliver its own required-extension errors.
    if (result.errors.length) return;
    if (profile.getState().tier === "full") return;
    const before = [...result.extensions];
    for (const source of state.sources)
      if (!before.some(entry => entry.resolvedPath === source.path)) throw new Error("Native loaded identity is missing");
    const final = await this.loadExtensionFactories.call({cwd:this.cwd, eventBus:this.eventBus,
      extensionFactories:[{name:"omnigent-native-final", factory(api) {
        state.finalHook = async (event, ctx) => {
          state.bind(store.sessions.get(ctx.sessionManager));
          const current = profile.getState();
          if (!current || current.status !== "ready") return {block:true, reason:"Native permission state is unavailable"};
          if (current.tier === "full") return evaluate ? nativeSupport.policyVerdict(await evaluate(event, ctx, null)) : undefined;
          const support = await nativeSupport.classifyNativeCall(event, ctx, current);
          if (support.status === "failed_native") return {block:true, reason:support.reason};
          return nativeSupport.evaluateNativeCall(event, ctx, support);
        };
        api.on("tool_call", state.finalHook);
        api.on("session_shutdown", (_event, ctx) => nativeSupport.closeNativeSession(ctx.sessionManager));
      }}]}, result.runtime);
    if (final.errors.length || final.extensions.length !== 1) throw new Error("Native final hook could not be installed");
    state.finalExtension = final.extensions[0];
    result.extensions.push(state.finalExtension);
    if (before.some((entry, index) => result.extensions[index] !== entry)) throw new Error("Native bootstrap changed SDK extension order");
    state.bind = actual => {
      if (!actual || actual.resourceLoader !== this) throw new Error("Native SDK session/loader identity is missing");
      if (state.manager === actual.sessionManager) return;
      const descriptor = describeSession(actual, state);
      nativeSupport.bindNativeSession(actual, {...descriptor, sources:state.sources,
        getState:profile.getState, bridgeDir:profile.bridgeDir || descriptor.bridgeDir,
        isInstalled:() => installation.active,
        evaluateScope:evaluate || descriptor.evaluateScope,
        finalExtension:state.finalExtension, finalHook:state.finalHook});
      state.manager = actual.sessionManager;
    };
  };
  for (const [name, implementation] of Object.entries(wraps)) loader[name] = implementation;
  const bindWrapped = async function (...args) {
    store.sessions.set(this.sessionManager, this);
    const state = store.loaders.get(this.resourceLoader);
    if (!state) throw new Error("Native SDK session was created without independent bootstrap");
    const result = await bind.apply(this, args);
    if (state.profile.getState().tier !== "full") state.bind(this);
    return result;
  };
  session.bindExtensions = bindWrapped;
  return () => {
    installation.active = false;
    for (const [name, implementation] of Object.entries(wraps))
      if (loader[name] === implementation) loader[name] = original[name];
    if (session.bindExtensions === bindWrapped) session.bindExtensions = bind;
  };
}
