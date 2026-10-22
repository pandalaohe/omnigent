import {
  DIALOG_SELECTOR,
  elementTarget,
  isActiveTarget,
  metrics,
  pageEvidence,
  rectOf,
  selectedText,
} from "./dom";
import type { Selection, TargetV1 } from "../protocol";

function startPicker(): void {
  if (
    window.top !== window ||
    document.contentType === "application/pdf" ||
    document.querySelector('embed[type="application/pdf"]')
  )
    return;
  const isolated = globalThis as typeof globalThis & {
    __omnigentAnnotationPickerCleanup?: () => void;
  };
  isolated.__omnigentAnnotationPickerCleanup?.();
  let generation = "";
  let revision = 0;
  let mode: Selection["kind"] = "element";
  let selected: Selection | undefined;
  let target: Element | undefined;
  let textRange: Range | undefined;
  let frozenSelection = "";
  let capturing = false;
  let regionStart: { x: number; y: number; pointerId: number } | undefined;
  const cleanup = new AbortController();
  const layer = document.createElement("div");
  layer.setAttribute("popover", "manual");
  layer.style.cssText =
    "position:fixed;inset:0;width:100vw;height:100vh;max-width:none;max-height:none;margin:0;border:0;padding:0;background:transparent;pointer-events:none;overflow:visible";
  const shadow = layer.attachShadow({ mode: "closed" });
  const style = document.createElement("style");
  style.textContent = `:host::backdrop{display:none}*{box-sizing:border-box} .bar{position:fixed;top:12px;left:12px;max-width:calc(100vw - 24px);padding:8px;display:flex;flex-wrap:wrap;gap:6px;background:#f7faff;color:#152845;border:1px solid #b8c8df;border-radius:8px;box-shadow:0 3px 16px #15284525;font:13px/1.4 system-ui,sans-serif;pointer-events:auto}button{font:inherit;padding:6px 10px;background:white;color:#152845;border:1px solid #b8c8df;border-radius:4px;cursor:pointer}button[aria-pressed=true]{background:#245fd6;color:white}button:focus-visible{outline:3px solid #245fd6;outline-offset:2px}.box{position:fixed;border:2px solid #245fd6;background:#245fd612;pointer-events:none}.help{flex-basis:100%;max-width:360px;overflow-wrap:anywhere}`;
  const bar = document.createElement("div");
  bar.className = "bar";
  bar.setAttribute("role", "toolbar");
  bar.setAttribute("aria-label", "Pick page evidence");
  const box = document.createElement("div");
  box.className = "box";
  box.hidden = true;
  const regionSurface = document.createElement("div");
  regionSurface.className = "region-surface";
  regionSurface.style.cssText =
    "position:fixed;inset:0;background:transparent;pointer-events:auto;touch-action:none;user-select:none;cursor:crosshair";
  regionSurface.hidden = true;
  const help = document.createElement("div");
  help.className = "help";
  help.setAttribute("role", "status");
  help.textContent =
    "Click an element. Escape closes the picker. Page content is untrusted evidence.";
  const button = (label: string, action: () => void) => {
    const node = document.createElement("button");
    node.type = "button";
    node.textContent = label;
    node.addEventListener(
      "click",
      (event) => {
        if (event.isTrusted && !capturing) action();
      },
      { signal: cleanup.signal },
    );
    bar.appendChild(node);
    return node;
  };
  const elementButton = button("Element", () => setMode("element"));
  const regionButton = button("Region", () => setMode("region"));
  const textButton = button("Text", () => setMode("text"));
  const captureTextButton = button("Capture selected text", () => {
    try {
      const text = selectedText();
      textRange = text.range;
      frozenSelection = getSelection()?.toString() ?? "";
      target = undefined;
      emit({
        kind: "text",
        target: text.target,
        rect: rectOf(text.range.getBoundingClientRect()),
        page: pageEvidence(),
        metrics: metrics(revision),
      });
    } catch (error) {
      help.textContent = error instanceof Error ? error.message : "Text selection failed";
    }
  });
  captureTextButton.addEventListener(
    "mousedown",
    (event) => {
      if (event.isTrusted) event.preventDefault();
    },
    { signal: cleanup.signal },
  );
  button("Close", dispose);
  bar.appendChild(help);
  shadow.append(style, regionSurface, box, bar);
  document.body.appendChild(layer);

  function setMode(next: typeof mode) {
    cancelRegion();
    mode = next;
    regionSurface.hidden = mode !== "region";
    elementButton.setAttribute("aria-pressed", String(mode === "element"));
    regionButton.setAttribute("aria-pressed", String(mode === "region"));
    textButton.setAttribute("aria-pressed", String(mode === "text"));
    help.textContent =
      mode === "region"
        ? "Drag a visible region. Element details inside frames or closed shadow trees are unavailable."
        : mode === "text"
          ? "Select visible text in the page, then click Capture selected text. Form controls are excluded."
          : "Click an element. Open shadow trees have no stable CSS selector.";
  }
  setMode("element");

  function place(element?: Element) {
    const host =
      element?.closest(DIALOG_SELECTOR) ??
      document.querySelector("dialog[open]:modal") ??
      document.body;
    if (layer.parentElement !== host) {
      if (layer.matches(":popover-open")) layer.hidePopover();
      host.appendChild(layer);
    }
    if (!layer.matches(":popover-open")) layer.showPopover();
  }

  function draw(rect: Selection["rect"]) {
    box.hidden = false;
    Object.assign(box.style, {
      left: `${rect.x}px`,
      top: `${rect.y}px`,
      width: `${rect.width}px`,
      height: `${rect.height}px`,
    });
  }

  function emit(selection: Selection) {
    if (!generation || capturing) return;
    selected = selection;
    capturing = true;
    help.textContent = "Capturing visible evidence…";
    chrome.runtime
      .sendMessage({ type: "pick", generation, selection })
      .then((response) => {
        const ok = response && typeof response === "object" && "ok" in response && response.ok;
        help.textContent = ok
          ? "Preview is in the side panel. Review it before use."
          : "Screenshot failed. See the side panel, then select again.";
      })
      .catch(() => {
        help.textContent = "Extension disconnected. Invoke Pick page evidence again.";
      })
      .finally(() => {
        capturing = false;
      });
  }

  function pickedElement(event: PointerEvent): Element | undefined {
    if (event.composedPath().includes(layer)) return undefined;
    const element = event.composedPath().find((node): node is Element => node instanceof Element);
    return element && isActiveTarget(element) ? element : undefined;
  }

  function move(event: PointerEvent) {
    if (!event.isTrusted || !generation || capturing || mode !== "element") return;
    const element = pickedElement(event);
    if (element) {
      place(element);
      draw(rectOf(element.getBoundingClientRect()));
    }
  }

  function regionDown(event: PointerEvent) {
    if (
      !event.isTrusted ||
      !generation ||
      capturing ||
      event.button !== 0 ||
      mode !== "region" ||
      regionStart
    )
      return;
    event.preventDefault();
    event.stopImmediatePropagation();
    try {
      regionSurface.setPointerCapture(event.pointerId);
      regionStart = { x: event.clientX, y: event.clientY, pointerId: event.pointerId };
    } catch {
      help.textContent = "Region drag ended. Drag again.";
    }
  }

  function cancelRegion() {
    const pointerId = regionStart?.pointerId;
    regionStart = undefined;
    box.hidden = true;
    if (pointerId !== undefined && regionSurface.hasPointerCapture(pointerId))
      regionSurface.releasePointerCapture(pointerId);
  }

  function regionMove(event: PointerEvent) {
    if (!event.isTrusted || !regionStart || event.pointerId !== regionStart.pointerId) return;
    draw({
      x: Math.min(regionStart.x, event.clientX),
      y: Math.min(regionStart.y, event.clientY),
      width: Math.abs(event.clientX - regionStart.x),
      height: Math.abs(event.clientY - regionStart.y),
    });
  }

  function regionUp(event: PointerEvent) {
    if (
      !event.isTrusted ||
      !generation ||
      capturing ||
      event.button !== 0 ||
      mode !== "region" ||
      !regionStart ||
      event.pointerId !== regionStart.pointerId
    )
      return;
    event.preventDefault();
    event.stopImmediatePropagation();
    textRange = undefined;
    const rect = {
      x: Math.min(regionStart.x, event.clientX),
      y: Math.min(regionStart.y, event.clientY),
      width: Math.abs(event.clientX - regionStart.x),
      height: Math.abs(event.clientY - regionStart.y),
    };
    cancelRegion();
    target = undefined;
    if (rect.width < 2 || rect.height < 2) {
      help.textContent = "Drag a larger region.";
      return;
    }
    const empty: TargetV1 = {
      tag: "",
      selector: "",
      quote: "",
      prefix: "",
      suffix: "",
      aria_label: "",
      role: "",
    };
    emit({
      kind: "region",
      page: pageEvidence(),
      target: empty,
      rect,
      metrics: metrics(revision),
    });
  }

  function up(event: PointerEvent) {
    if (
      !event.isTrusted ||
      !generation ||
      capturing ||
      event.button !== 0 ||
      mode !== "element" ||
      event.composedPath().includes(layer)
    )
      return;
    event.preventDefault();
    event.stopImmediatePropagation();
    textRange = undefined;
    const element = pickedElement(event);
    if (!element) return;
    target = element;
    emit({
      kind: "element",
      page: pageEvidence(),
      target: elementTarget(element),
      rect: rectOf(element.getBoundingClientRect()),
      metrics: metrics(revision),
    });
  }

  function measure(): Selection {
    if (!selected) throw new Error("No frozen selection");
    if (target && !isActiveTarget(target)) throw new Error("Target disappeared");
    if (textRange) {
      const current = getSelection();
      const range = current?.rangeCount === 1 ? current.getRangeAt(0) : undefined;
      if (
        current?.toString() !== frozenSelection ||
        !range ||
        range.startContainer !== textRange.startContainer ||
        range.startOffset !== textRange.startOffset ||
        range.endContainer !== textRange.endContainer ||
        range.endOffset !== textRange.endOffset
      )
        throw new Error("Text selection changed");
    }
    return {
      ...selected,
      page: pageEvidence(),
      target: target ? elementTarget(target) : selected.target,
      rect: target
        ? rectOf(target.getBoundingClientRect())
        : textRange
          ? rectOf(textRange.getBoundingClientRect())
          : selected.rect,
      metrics: metrics(revision),
    };
  }

  const frames = async () => {
    await new Promise(requestAnimationFrame);
    await new Promise(requestAnimationFrame);
  };
  const listener: Parameters<typeof chrome.runtime.onMessage.addListener>[0] = (
    input,
    sender,
    respond,
  ) => {
    if (
      sender.id !== chrome.runtime.id ||
      sender.tab ||
      (sender.url !== undefined && !sender.url.startsWith(chrome.runtime.getURL(""))) ||
      !input ||
      typeof input !== "object"
    )
      return;
    const message = input as { type?: unknown; generation?: unknown };
    if (message.type === "dispose") {
      dispose();
      respond({ ok: true });
      return;
    }
    if (message.type === "activate" && typeof message.generation === "string" && !generation) {
      generation = message.generation;
      void frames()
        .then(() => {
          place();
          respond({ ok: true });
        })
        .catch(() => {
          dispose();
          respond({ ok: false });
        });
      return true;
    }
    if (message.generation !== generation) return;
    if (message.type === "restore-overlay") {
      layer.style.visibility = "visible";
      respond({ ok: true });
    } else if (message.type === "measure-capture") {
      try {
        respond(measure());
      } catch {
        respond({ error: "Selection changed" });
      }
    } else if (message.type === "prepare-capture") {
      layer.style.visibility = "hidden";
      void frames()
        .then(() => respond(measure()))
        .catch(() => respond({ error: "Selection changed" }));
      return true;
    }
    return;
  };
  function dispose() {
    cancelRegion();
    cleanup.abort();
    chrome.runtime.onMessage.removeListener(listener);
    layer.remove();
    if (isolated.__omnigentAnnotationPickerCleanup === dispose)
      delete isolated.__omnigentAnnotationPickerCleanup;
  }
  isolated.__omnigentAnnotationPickerCleanup = dispose;
  const changed = () => {
    revision++;
  };
  document.addEventListener("pointermove", move, {
    capture: true,
    signal: cleanup.signal,
  });
  // Inside the closed shadow, the surface remains distinguishable from the toolbar.
  regionSurface.addEventListener("pointerdown", regionDown, { signal: cleanup.signal });
  regionSurface.addEventListener("pointermove", regionMove, { signal: cleanup.signal });
  regionSurface.addEventListener("pointerup", regionUp, { signal: cleanup.signal });
  for (const type of ["pointercancel", "lostpointercapture"])
    regionSurface.addEventListener(
      type,
      (event) => {
        if (event.isTrusted && (event as PointerEvent).pointerId === regionStart?.pointerId)
          cancelRegion();
      },
      { signal: cleanup.signal },
    );
  window.addEventListener("blur", cancelRegion, { signal: cleanup.signal });
  document.addEventListener("pointerup", up, {
    capture: true,
    signal: cleanup.signal,
  });
  document.addEventListener(
    "click",
    (event) => {
      if (
        event.isTrusted &&
        generation &&
        mode !== "text" &&
        !event.composedPath().includes(layer)
      ) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    },
    { capture: true, signal: cleanup.signal },
  );
  document.addEventListener(
    "keydown",
    (event) => {
      if (event.isTrusted && event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        dispose();
      }
    },
    { capture: true, signal: cleanup.signal },
  );
  document.addEventListener("scroll", changed, {
    capture: true,
    signal: cleanup.signal,
  });
  document.addEventListener(
    "visibilitychange",
    () => {
      changed();
      if (document.visibilityState !== "visible") cancelRegion();
    },
    {
      signal: cleanup.signal,
    },
  );
  window.addEventListener("resize", changed, { signal: cleanup.signal });
  visualViewport?.addEventListener("resize", changed, {
    signal: cleanup.signal,
  });
  visualViewport?.addEventListener("scroll", changed, {
    signal: cleanup.signal,
  });
  chrome.runtime.onMessage.addListener(listener);
}

startPicker();
