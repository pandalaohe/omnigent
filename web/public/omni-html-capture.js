// Early in-frame capture script for the HTML artifact preview: the server
// inlines these bytes right after <head> and htmlCommentBridge.ts imports them
// ?raw for the srcdoc path. It must run before page scripts, or load-time
// errors, failed requests and early keydown listeners would be missed. Portions
// are a port of SitePing diagnostics/{console-buffer,network-buffer,truncate}.ts
// (MIT, commit 3df02534); the licence text is in
// web/src/shell/annotate/runtime/THIRD_PARTY_NOTICES.md. Dependency-free; it
// runs in the artifact's opaque-origin frame and no-ops without a nonce.

(function () {
  var NONCE = (document.currentScript && document.currentScript.dataset.omniNonce) || "";
  if (!NONCE) return;
  // The namespace property below is non-configurable: a second copy of the
  // script (or a page-defined value) must not make its definition throw.
  if (window["__omniCapture"]) return;

  // Caps mirror the anchor payload (design §2.6) and the SitePing buffers.
  var MAX_CONSOLE = 50;
  var MAX_MESSAGE = 500;
  var MAX_NETWORK = 20;
  var MAX_URL = 2000;
  var MAX_METHOD = 20;
  var MAX_STATUS = 599;

  var consoleEntries = [];
  var networkEntries = [];

  // One capture-phase listener per interactive type, all registered here before
  // any page script, so a runtime handler installed through setSlot wins over
  // page listeners registered later. The annotate stub takes `keydown`; the
  // picker and the freeze take the rest while the mode is on. The hover and
  // focus types are here because a page handler registered before mode entry
  // would otherwise see the pointer or focus leaving the menu and close it.
  var SLOT_TYPES = [
    "pointermove",
    "pointerdown",
    "pointerup",
    "click",
    "mousedown",
    "mouseup",
    "contextmenu",
    "dblclick",
    "keydown",
    "pointerover",
    "pointerout",
    "pointerenter",
    "pointerleave",
    "mouseover",
    "mouseout",
    "mouseenter",
    "mouseleave",
    "focus",
    "blur",
    "focusin",
    "focusout",
  ];
  var slots = {};

  // SitePing truncate.ts: the cut never splits a surrogate pair, which would
  // leave a lone surrogate that breaks JSON encoding downstream.
  function truncateWithEllipsis(text, maxLength) {
    if (text.length <= maxLength) return text;
    var end = maxLength - 1;
    var last = text.charCodeAt(end - 1);
    if (last >= 0xd800 && last <= 0xdbff) end -= 1;
    return text.slice(0, end) + "…";
  }

  // SitePing console-buffer serializeArg: best-effort stringification that
  // never throws. Cycles are detected against the ancestor chain, a node
  // budget bounds diamond-shaped graphs, and functions/symbols are replaced.
  function serializeArg(arg) {
    if (arg === null) return "null";
    if (arg === undefined) return "undefined";
    if (typeof arg === "string") return arg;
    if (typeof arg === "number" || typeof arg === "boolean" || typeof arg === "bigint") {
      return String(arg);
    }
    if (arg instanceof Error) {
      return arg.name + ": " + arg.message + (arg.stack ? "\n" + arg.stack : "");
    }
    try {
      var ancestors = [];
      var budget = MAX_MESSAGE;
      var out = JSON.stringify(arg, function (_key, value) {
        if (--budget < 0) return undefined;
        if (typeof value === "function") return "[Function]";
        if (typeof value === "symbol") return value.toString();
        if (typeof value !== "object" || value === null) return value;
        while (ancestors.length > 0 && ancestors[ancestors.length - 1] !== this) ancestors.pop();
        if (ancestors.indexOf(value) !== -1) return "[Circular]";
        ancestors.push(value);
        return value;
      });
      return out === undefined ? String(arg) : out;
    } catch {
      try {
        return String(arg);
      } catch {
        return "[Unserializable]";
      }
    }
  }

  function formatArgs(args) {
    var out = "";
    for (var i = 0; i < args.length; i++) {
      if (i > 0) out += " ";
      out += serializeArg(args[i]);
      if (out.length >= MAX_MESSAGE) break;
    }
    return truncateWithEllipsis(out, MAX_MESSAGE);
  }

  function pushConsole(level, message) {
    if (consoleEntries.length >= MAX_CONSOLE) consoleEntries.shift();
    consoleEntries.push({
      level: level,
      message: truncateWithEllipsis(String(message), MAX_MESSAGE),
      ts: Date.now(),
    });
  }

  function pushNetwork(method, url, status) {
    if (networkEntries.length >= MAX_NETWORK) networkEntries.shift();
    networkEntries.push({
      method: String(method).slice(0, MAX_METHOD),
      url: truncateWithEllipsis(String(url), MAX_URL),
      status: typeof status === "number" && status >= 0 && status <= MAX_STATUS ? status : 0,
      ts: Date.now(),
    });
  }

  // Only error and warn are captured: log/info would flood the ring, and the
  // payload carries faults only.
  function wrapConsole(level) {
    if (typeof console === "undefined" || typeof console[level] !== "function") return;
    var original = console[level];
    var wrapped = function () {
      try {
        pushConsole(level, formatArgs(arguments));
      } catch {
        // Capturing must never break the host's console call.
      }
      return original.apply(this, arguments);
    };
    try {
      Object.defineProperty(wrapped, "name", { value: level });
    } catch {
      // Older engines reject the redefinition; only devtools naming suffers.
    }
    console[level] = wrapped;
  }

  wrapConsole("error");
  wrapConsole("warn");

  // Native URL and Request accessors, captured while the globals are still the
  // originals. Metadata reads use these instead of ordinary property lookups,
  // so an accessor a page defines on the object itself never runs; a brand
  // mismatch throws and reads as "not that type".
  function nativeAccessor(owner, property) {
    if (!owner || !owner.prototype) return null;
    var descriptor = Object.getOwnPropertyDescriptor(owner.prototype, property);
    return descriptor && typeof descriptor.get === "function" ? descriptor.get : null;
  }

  var NativeURL = typeof URL !== "undefined" ? URL : null;
  var NativeRequest = typeof Request !== "undefined" ? Request : null;
  var urlHrefGetter = nativeAccessor(NativeURL, "href");
  var requestUrlGetter = nativeAccessor(NativeRequest, "url");
  var requestMethodGetter = nativeAccessor(NativeRequest, "method");

  function nativeString(getter, value) {
    if (typeof getter !== "function") return null;
    try {
      var result = getter.call(value);
      return typeof result === "string" ? result : null;
    } catch {
      return null;
    }
  }

  // The url as recorded: credentials, query and hash dropped, only http(s)
  // kept (design §2.6 — anything else, including unparseable input, is "").
  // Reads only strings, URLs and Requests: a page-passed object's getters (or
  // coercions) must never run, or metadata capture would change fetch itself.
  function recordableUrl(input) {
    var raw = null;
    if (typeof input === "string") raw = input;
    else if (typeof input === "object" && input !== null) {
      raw = nativeString(urlHrefGetter, input);
      if (raw === null) raw = nativeString(requestUrlGetter, input);
    }
    if (raw === null) return "";
    var parsed;
    try {
      parsed = new NativeURL(raw, location.href);
    } catch {
      return "";
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return "";
    return truncateWithEllipsis(parsed.protocol + "//" + parsed.host + parsed.pathname, MAX_URL);
  }

  function resourceUrl(target) {
    if (typeof target.currentSrc === "string" && target.currentSrc) return target.currentSrc;
    if (typeof target.src === "string" && target.src) return target.src;
    if (typeof target.href === "string" && target.href) return target.href;
    return "";
  }

  window.addEventListener(
    "error",
    function (event) {
      try {
        var target = event.target;
        // A resource error's target is the failed element; uncaught script
        // errors arrive at window with a message/filename instead.
        if (
          target &&
          target !== window &&
          target.nodeType === 1 &&
          (typeof target.src === "string" || typeof target.href === "string")
        ) {
          var url = recordableUrl(resourceUrl(target));
          pushConsole("error", "Failed to load " + target.tagName.toLowerCase() + " " + url);
          pushNetwork("GET", url, 0);
          return;
        }
        var message = event.message ? String(event.message) : "Uncaught error";
        if (event.filename) message += " " + String(event.filename) + ":" + (event.lineno || 0);
        pushConsole("error", message);
      } catch {
        // An error listener must never throw into the page.
      }
    },
    true,
  );

  window.addEventListener("unhandledrejection", function (event) {
    try {
      pushConsole("error", serializeArg(event.reason));
    } catch {
      // Same rule: never surface a capture failure to the page.
    }
  });

  if (typeof window.fetch === "function") {
    var originalFetch = window.fetch;
    window.fetch = function (input, init) {
      var url = "";
      var method = "GET";
      try {
        url = recordableUrl(input);
        // Only an own data `method` is safe to read: a page-defined getter must
        // never run here. A Request's method comes from the captured accessor.
        var descriptor =
          init && typeof init === "object"
            ? Object.getOwnPropertyDescriptor(init, "method")
            : undefined;
        var initMethod = descriptor && "value" in descriptor ? descriptor.value : undefined;
        if (typeof initMethod === "string" && initMethod) {
          method = initMethod.toUpperCase();
        } else if (initMethod === undefined) {
          var requestMethod = nativeString(requestMethodGetter, input);
          if (requestMethod) method = requestMethod.toUpperCase();
        }
      } catch {
        // Metadata is best-effort; the native call below is authoritative and
        // must see the untouched input, `this` and arguments.
        url = "";
        method = "GET";
      }
      var result;
      try {
        result = originalFetch.apply(this, arguments);
      } catch (err) {
        pushNetwork(method, url, 0);
        throw err;
      }
      if (!result || typeof result.then !== "function") return result;
      return result.then(
        function (response) {
          try {
            if (!response.ok) pushNetwork(method, url, response.status);
          } catch {
            // A response whose ok/status access throws stays untouched.
          }
          return response;
        },
        function (err) {
          pushNetwork(method, url, 0);
          throw err;
        },
      );
    };
  }

  if (typeof XMLHttpRequest !== "undefined") {
    var xhrProto = XMLHttpRequest.prototype;
    var originalOpen = xhrProto.open;
    var originalSend = xhrProto.send;
    if (typeof originalOpen === "function" && typeof originalSend === "function") {
      // Metadata per instance, so concurrent opens on one object cannot
      // overwrite each other's request record.
      var meta = new WeakMap();
      xhrProto.open = function (method, url) {
        try {
          meta.set(this, { method: String(method).toUpperCase(), url: recordableUrl(url) });
        } catch {
          // Metadata is best-effort; the underlying open() still runs.
        }
        return originalOpen.apply(this, arguments);
      };
      xhrProto.send = function () {
        var info = meta.get(this);
        if (info) {
          // loadend fires on success, network error and abort alike; only a
          // status 0 or >= 400 is recorded.
          var xhr = this;
          var onEnd = function () {
            try {
              var status = xhr.status;
              if (status === 0 || status >= 400) pushNetwork(info.method, info.url, status);
            } catch {
              // The listener must not throw into the page.
            }
          };
          try {
            this.addEventListener("loadend", onEnd, { once: true });
          } catch {
            try {
              this.addEventListener("loadend", onEnd);
            } catch {
              // No usable listener API; the request itself still proceeds.
            }
          }
        }
        return originalSend.apply(this, arguments);
      };
    }
  }

  for (var slotIndex = 0; slotIndex < SLOT_TYPES.length; slotIndex++) {
    (function (type) {
      window.addEventListener(
        type,
        function (event) {
          var slot = slots[type];
          if (!slot) return;
          try {
            slot(event);
          } catch {
            // A throwing slot must not break the page's own handling.
          }
        },
        true,
      );
    })(SLOT_TYPES[slotIndex]);
  }

  Object.defineProperty(window, "__omniCapture", {
    value: {
      snapshot: function () {
        return {
          console: consoleEntries.map(function (entry) {
            return { level: entry.level, message: entry.message, ts: entry.ts };
          }),
          network: networkEntries.map(function (entry) {
            return { method: entry.method, url: entry.url, status: entry.status, ts: entry.ts };
          }),
        };
      },
      setSlot: function (type, slot) {
        if (SLOT_TYPES.indexOf(type) === -1) return;
        slots[type] = typeof slot === "function" ? slot : null;
      },
    },
    configurable: false,
    writable: false,
    enumerable: false,
  });
})();
