const nativeSupport = require("./native_support.cjs");

function createChildNativeGate({readState, evaluate}) {
  return async (event, ctx) => {
    const state = readState(ctx);
    if (!state || state.status !== "ready") return {block:true, reason:"Native permission state is unavailable"};
    if (state.tier === "full") return evaluate ? nativeSupport.policyVerdict(await evaluate(event, ctx, null)) : undefined;
    const support = await nativeSupport.classifyNativeCall(event, ctx, state);
    if (support.status === "failed_native") return {block:true, reason:support.reason};
    // The scoped consumer supplies this callback; there is no parent-token fallback.
    if (typeof evaluate === "function") return nativeSupport.policyVerdict(await evaluate(event, ctx, support));
    if (nativeSupport.hasScopedEvaluator(ctx)) return;
    return {block:true, reason:"Scoped native policy transport is not installed"};
  };
}

module.exports = function (pi) {
  pi.on("session_start", (_event, ctx) => nativeSupport.requireNativeSession(ctx));
  pi.on("tool_call", createChildNativeGate({readState:nativeSupport.getNativeState}));
};
module.exports.createChildNativeGate = createChildNativeGate;
