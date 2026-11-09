/*
 * Storage-event workaround adapted from chiaroscuro2, src/preload/extension-api.ts.
 * https://github.com/MortenChristiansenOrg/chiaroscuro2
 * Copyright (c) 2026 Morten Christiansen
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */
// Electron omits native storage change events in MV3 workers. Values stay in the extension.
const { contextBridge, ipcRenderer } = require("electron");

const extensionId = contextBridge.executeInMainWorld({
  func: () => globalThis.chrome?.runtime?.id,
});
if (typeof extensionId === "string" && /^[a-p]{32}$/.test(extensionId)) {
  contextBridge.exposeInMainWorld("omnigentExtensionStorage", {
    changed: (keys, area) => ipcRenderer.send("omnigent:extension-storage", keys, area),
    requests: (callback) =>
      ipcRenderer.on("omnigent:extension-request", (_event, name, details) =>
        callback(name, details),
      ),
    subscribe: (callback) => {
      ipcRenderer.on("omnigent:extension-storage", (_event, keys, area) => callback(keys, area));
      ipcRenderer.send("omnigent:extension-storage-subscribe");
    },
  });
  contextBridge.executeInMainWorld({
    func: () => {
      const chrome = globalThis.chrome;
      const bridge = globalThis.omnigentExtensionStorage;
      if (!chrome.storage) return;
      if (typeof document !== "undefined") {
        chrome.storage.onChanged.addListener((changes, area) => {
          if (area === "local" || area === "session") bridge.changed(Object.keys(changes), area);
        });
        return;
      }
      const event = () => {
        const callbacks = new Set();
        return {
          callbacks,
          addListener: (cb) => callbacks.add(cb),
          removeListener: (cb) => callbacks.delete(cb),
          hasListener: (cb) => callbacks.has(cb),
          hasListeners: () => callbacks.size > 0,
        };
      };
      const requests = new Map();
      const matchesRequest = (pattern, rawUrl) => {
        let url;
        try {
          url = new URL(rawUrl);
        } catch {
          return false;
        }
        if (!["http:", "https:"].includes(url.protocol)) return false;
        if (pattern === "<all_urls>") return true;
        const match = /^(\*|https?):\/\/([^/]+)(\/.*)$/.exec(pattern);
        if (!match || (match[1] !== "*" && `${match[1]}:` !== url.protocol)) return false;
        const authority = /^(\[[^\]]+\]|[^:]+)(?::(\*|\d+))?$/.exec(match[2]);
        if (!authority) return false;
        const host = authority[1].toLowerCase();
        if (
          host !== "*" &&
          host !== url.hostname &&
          !(
            host.startsWith("*.") &&
            (url.hostname === host.slice(2) || url.hostname.endsWith(host.slice(1)))
          )
        )
          return false;
        if (
          authority[2] &&
          authority[2] !== "*" &&
          authority[2] !== (url.port || (url.protocol === "https:" ? "443" : "80"))
        )
          return false;
        const path = match[3]
          .split("*")
          .map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
          .join(".*");
        return new RegExp(`^${path}$`).test(url.pathname + url.search);
      };
      if (chrome.webRequest) {
        for (const name of [
          "onBeforeRequest",
          "onCompleted",
          "onBeforeRedirect",
          "onErrorOccurred",
        ]) {
          const listeners = new Map();
          requests.set(name, listeners);
          Object.defineProperty(chrome.webRequest, name, {
            configurable: true,
            value: {
              addListener: (cb, filter = {}) => listeners.set(cb, filter),
              removeListener: (cb) => listeners.delete(cb),
              hasListener: (cb) => listeners.has(cb),
              hasListeners: () => listeners.size > 0,
            },
          });
        }
        bridge.requests((name, details) => {
          for (const [cb, filter] of requests.get(name) ?? []) {
            if (filter.types && !filter.types.includes(details.type)) continue;
            if (filter.urls && !filter.urls.some((pattern) => matchesRequest(pattern, details.url)))
              continue;
            cb(details);
          }
        });
      }
      const global = event();
      Object.defineProperty(chrome.storage, "onChanged", { configurable: true, value: global });
      const areas = new Map();
      let pending = Promise.resolve();
      for (const name of ["local", "session"]) {
        const area = chrome.storage[name];
        if (!area) continue;
        const get = area.get.bind(area);
        const changed = event();
        Object.defineProperty(area, "onChanged", { configurable: true, value: changed });
        const state = { get, changed, known: {} };
        areas.set(name, state);
        pending = pending.then(async () => {
          state.known = await get(null);
        });
        for (const operation of ["set", "remove", "clear"]) {
          const native = area[operation].bind(area);
          area[operation] = (...args) => {
            const callback = typeof args.at(-1) === "function" ? args.pop() : undefined;
            const promise = pending.then(async () => {
              const keys =
                operation === "set"
                  ? Object.keys(args[0])
                  : operation === "remove"
                    ? [].concat(args[0])
                    : Object.keys(await get(null));
              await native(...args);
              bridge.changed(keys, name);
            });
            if (!callback) return promise;
            promise.then(callback, () => callback());
            return undefined;
          };
        }
      }
      bridge.subscribe((keys, name) => {
        const state = areas.get(name);
        if (!state) return;
        pending = pending
          .then(async () => {
            const current = await state.get(keys);
            const changes = {};
            for (const key of keys) {
              const oldValue = state.known[key];
              if (JSON.stringify(oldValue) === JSON.stringify(current[key])) continue;
              changes[key] = {
                ...(key in state.known ? { oldValue } : {}),
                ...(key in current ? { newValue: current[key] } : {}),
              };
              if (key in current) state.known[key] = current[key];
              else Reflect.deleteProperty(state.known, key);
            }
            if (!Object.keys(changes).length) return;
            for (const cb of state.changed.callbacks) cb(changes);
            for (const cb of global.callbacks) cb(changes, name);
          })
          .catch(() => {});
      });
    },
  });
}
