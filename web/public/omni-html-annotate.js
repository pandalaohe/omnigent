// In-frame element-annotation stub for the HTML artifact preview: the server
// inlines these bytes and htmlCommentBridge.ts imports them ?raw. It owns the
// annotate MessagePort, evaluates the runtime parts it is sent, and routes
// every other message to the handler the runtime registers for that type.
// Dependency-free; it runs in the artifact's opaque-origin frame and no-ops
// without a nonce.

(function () {
  var NONCE = (document.currentScript && document.currentScript.dataset.omniNonce) || "";
  if (!NONCE) return;
  // The namespace property below is non-configurable: a second copy of the
  // stub (or a page-defined value) must not make its definition throw.
  if (window["__omniAnnotate"]) return;

  var SRC = "omni-html-annotate";
  var T = {
    init: "annotate:init",
    ready: "annotate:ready",
    loadRuntime: "annotate:loadRuntime",
    runtimeLoaded: "annotate:runtimeLoaded",
    setShortcut: "annotate:setShortcut",
    toggleRequested: "annotate:toggleRequested",
  };

  var port = null;
  var handlers = {}; // one handler per inbound type, registered by the runtime
  var shortcuts = []; // bindings: {code, ctrl, meta, alt, shift}

  function send(msg) {
    if (!port) return;
    msg.source = SRC;
    msg.nonce = NONCE;
    try {
      port.postMessage(msg);
    } catch {
      // The parent may have closed the port between messages; dropping is safe.
    }
  }

  var ns = {
    nonce: NONCE,
    send: send,
    on: function (type, handler) {
      handlers[type] = handler;
    },
    loaded: { core: false, pick: false },
  };
  Object.defineProperty(window, "__omniAnnotate", {
    value: ns,
    configurable: false,
    writable: false,
    enumerable: false,
  });

  // Evaluate one runtime source as an inline <script>. A runtime file is the
  // body of `function (ns) { … }`. Success is recorded only by the source's
  // last statement, so a throw, a syntax error or a page CSP that blocks the
  // script all read as failure without touching window.onerror.
  function evalSource(source) {
    ns.runtimeEvalOk = false;
    var script = document.createElement("script");
    script.textContent =
      "(function(ns){try{\n" +
      source +
      "\n;ns.runtimeEvalOk=true;}catch(e){}})(window.__omniAnnotate);";
    (document.head || document.documentElement).appendChild(script);
    script.remove();
    return ns.runtimeEvalOk === true;
  }

  function onLoadRuntime(msg) {
    var sources = Array.isArray(msg.sources) ? msg.sources : [];
    var ok = true;
    for (var i = 0; i < sources.length; i++) {
      if (!evalSource(String(sources[i]))) ok = false;
    }
    if (ok && typeof msg.part === "string") ns.loaded[msg.part] = true;
    send({ type: T.runtimeLoaded, part: msg.part, ok: ok });
  }

  function onPortMessage(ev) {
    var m = ev.data;
    if (!m || typeof m.type !== "string") return;
    if (m.type === T.loadRuntime) {
      onLoadRuntime(m);
      return;
    }
    if (m.type === T.setShortcut) {
      shortcuts = Array.isArray(m.bindings) ? m.bindings : [];
      return;
    }
    var handler = handlers[m.type];
    if (handler) handler(m);
  }

  function matchesShortcut(event, binding) {
    return (
      event.code === binding.code &&
      event.ctrlKey === !!binding.ctrl &&
      event.metaKey === !!binding.meta &&
      event.altKey === !!binding.alt &&
      event.shiftKey === !!binding.shift
    );
  }

  // Capture-phase so the chord wins over page handlers while the artifact frame
  // has focus. Before the pick runtime exists it only asks the parent to load
  // it; once ns.toggleMode is exported that asks the parent to turn the mode on,
  // or leaves it in-frame when it is already on.
  function onKeydown(event) {
    if (event.isComposing) return;
    for (var i = 0; i < shortcuts.length; i++) {
      if (!matchesShortcut(event, shortcuts[i])) continue;
      event.preventDefault();
      event.stopImmediatePropagation();
      // Page scripts share the realm and can dispatch events; only the user's
      // own input (isTrusted) may toggle.
      if (!event.isTrusted) return;
      if (ns.loaded.pick && typeof ns.toggleMode === "function") ns.toggleMode("shortcut");
      else send({ type: T.toggleRequested });
      return;
    }
  }

  // The head capture script already owns the earliest capture-phase keydown
  // listener; route through its slot instead of adding a second one. The
  // picker, once loaded, appends its own handler to the same slot so the
  // shortcut is matched first. Without the capture script (old server, visitor
  // shell) listen directly.
  var capture = window["__omniCapture"];
  if (capture && typeof capture.setSlot === "function") {
    ns.diagnostics = capture;
    capture.setSlot("keydown", function (event) {
      onKeydown(event);
      if (typeof ns.pickerKeydown === "function") ns.pickerKeydown(event);
    });
  } else {
    window.addEventListener("keydown", onKeydown, true);
  }

  window.addEventListener("message", function (e) {
    var d = e.data;
    if (!d || d.source !== SRC || d.nonce !== NONCE) return;
    if (d.type !== T.init || !e.ports || !e.ports[0]) return;
    if (port) {
      try {
        port.close();
      } catch {
        // Already closed; the new port replaces it either way.
      }
    }
    port = e.ports[0];
    port.onmessage = onPortMessage;
    send({ type: T.ready, pathname: location.pathname });
  });
})();
