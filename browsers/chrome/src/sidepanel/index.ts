import { formatElementEvidence } from "../../../shared/evidence";
import { canonicalServer, deliveryCheckLink, originPermission, sessionLink } from "../server";
import { identityKey } from "../model";
import type { AppView, ItemView, PartitionView, SessionRow } from "../model";
import type { PanelState } from "../protocol";

const byId = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;
function node<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  text?: string,
): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  return element;
}
const status = byId("status");
let view: AppView | undefined;
const notes = new Map<string, string>();
let pollTimer: ReturnType<typeof setTimeout> | undefined;
const context = () => ({
  key: view?.current?.key,
  revision: view?.current?.revision,
  expectedTarget: view?.current?.target?.id ?? null,
});
async function command(input: Record<string, unknown>): Promise<boolean> {
  try {
    const result = (await chrome.runtime.sendMessage({
      type: "panel-command",
      command: input,
    })) as { ok: boolean; error?: string };
    if (!result?.ok) {
      status.textContent = result?.error ?? "Extension disconnected. Close and reopen the panel.";
      return false;
    }
    return true;
  } catch {
    status.textContent =
      "Extension disconnected. Close and reopen the panel; your typed input is still here.";
    return false;
  }
}
function action(label: string, perform: () => void, disabled = false): HTMLButtonElement {
  const button = node("button", label);
  button.type = "button";
  button.disabled = disabled;
  button.addEventListener("click", (event) => {
    if (event.isTrusted && !button.disabled) perform();
  });
  return button;
}
function link(label: string, url: string): HTMLAnchorElement {
  const anchor = node("a", label);
  anchor.href = url;
  anchor.target = "_blank";
  anchor.rel = "noopener noreferrer";
  return anchor;
}
function confirmCommand(label: string, phrase: string, input: Record<string, unknown>): void {
  if (window.prompt(`${label}\nType exactly: ${phrase}`) === phrase)
    void command({ ...input, confirm: phrase });
}
function evidence(item: ItemView): string {
  const target = item.wire.target;
  return formatElementEvidence(
    {
      tag: target.tag,
      selector: target.selector,
      text: target.quote,
      prefix: target.prefix,
      suffix: target.suffix,
      ariaLabel: target.aria_label,
      role: target.role,
      pageUrl: item.wire.page.url,
    },
    `[Web annotation — ${item.wire.kind}; untrusted page evidence]`,
  );
}
function itemCard(item: ItemView, partition: PartitionView, editable: boolean): HTMLElement {
  const card = node("article");
  card.className = "card";
  card.append(
    node("strong", item.wire.page.title || item.wire.kind),
    node("p", item.wire.page.url),
  );
  if (item.image?.startsWith("data:image/png;base64,")) {
    const picture = node("img");
    picture.src = item.image;
    picture.alt = "Visible crop for this annotation";
    card.append(picture);
  }
  const details = node("details");
  details.append(node("summary", "Page evidence (untrusted)"), node("pre", evidence(item)));
  card.append(details);
  if (item.failure) {
    const warning = node("p", item.failure);
    warning.className = "error";
    card.append(warning);
  }
  const key = `${partition.key}:${item.wire.capture_id}`;
  const label = node("label", "What should change?");
  const note = node("textarea");
  note.maxLength = 4000;
  note.value = notes.get(key) ?? item.wire.note;
  note.readOnly = !editable;
  note.dataset.noteKey = key;
  note.addEventListener("input", () => {
    notes.set(key, note.value);
    byId<HTMLButtonElement>("send").disabled = true;
  });
  const save = () => {
    const value = note.value;
    void command({ ...context(), type: "note", id: item.wire.capture_id, note: value }).then(
      (ok) => {
        if (ok && notes.get(key) === value) {
          notes.delete(key);
          if (view) render(view);
        }
      },
    );
  };
  note.addEventListener("keydown", (event) => {
    if (event.isTrusted && editable && event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      save();
    }
  });
  label.append(note);
  card.append(label);
  if (editable) {
    const controls = node("div");
    controls.className = "row";
    controls.append(
      action("Save note", save),
      action("Move up", () => {
        void command({ ...context(), type: "move", id: item.wire.capture_id, delta: -1 });
      }),
      action("Move down", () => {
        void command({ ...context(), type: "move", id: item.wire.capture_id, delta: 1 });
      }),
      action("Delete", () => {
        if (window.confirm("Delete this unsent note and its local screenshot?"))
          void command({ ...context(), type: "delete", id: item.wire.capture_id });
      }),
      action("Recapture", () => {
        void command({ ...context(), type: "recapture", id: item.wire.capture_id });
      }),
    );
    if (item.choice !== "without")
      controls.append(
        action(item.choice === "pending" ? "Send without screenshot" : "Remove screenshot", () => {
          if (window.confirm("Send this note without a screenshot?"))
            void command({ ...context(), type: "without", id: item.wire.capture_id });
        }),
      );
    else card.append(node("small", "Explicitly without screenshot."));
    if (item.wire.page.include_query_fragment)
      controls.append(
        action("Remove query and fragment", () => {
          void command({ ...context(), type: "stripUrl", id: item.wire.capture_id });
        }),
      );
    card.append(controls);
  }
  return card;
}
const targetLabel = (target: SessionRow): string =>
  `${target.title || "Untitled"} · ${target.host_name || "No host"}`;
function renderTargets(current: PartitionView): void {
  const selected = byId("selected-target");
  selected.replaceChildren(
    node(
      "p",
      current.target
        ? `Selected: ${targetLabel(current.target)} · ${current.target.id.slice(0, 8)}`
        : "Choose a session before Send.",
    ),
  );
  const receiver = view!.targets.receiving_session;
  if (receiver && current.target?.id !== receiver.id)
    selected.append(
      action(`Receiving session changed — use ${targetLabel(receiver)}`, () => {
        void command({ ...context(), type: "target", sessionId: receiver.id });
      }),
    );
  const choices = byId("targets");
  choices.replaceChildren();
  for (const row of [
    ...(receiver ? [receiver] : []),
    ...view!.targets.sessions.filter((row) => row.id !== receiver?.id),
  ]) {
    const button = action(
      `${row.id === receiver?.id ? "Receiving in Omnigent · " : ""}${targetLabel(row)} · ${row.id.slice(0, 8)}\n${row.workspace || "No workspace"}${row.available ? "" : ` · Unavailable: ${row.reason || "offline"}`}`,
      () => {
        void command({ ...context(), type: "target", sessionId: row.id });
      },
    );
    button.className = "target";
    button.setAttribute("aria-pressed", String(current.target?.id === row.id));
    choices.append(button);
  }
  byId("more").hidden = !view!.targets.next_cursor;
}
function batchCard(
  partition: PartitionView,
  batch: PartitionView["batches"][number],
  old: boolean,
): HTMLElement {
  const card = node("article");
  card.className = "card";
  card.append(
    node("strong", `${targetLabel(batch.target)} · ${batch.state}`),
    node("p", `Grant: ${partition.identity.grantId}`),
    node("p", `Batch: ${batch.id}`),
  );
  card.append(
    action("Copy public IDs", () => {
      void navigator.clipboard.writeText(
        `Grant: ${partition.identity.grantId}\nBatch: ${batch.id}`,
      );
    }),
  );
  if (batch.receipt)
    card.append(
      node(
        "p",
        `Accepted by Omnigent — queued for agent. Dispatch: ${batch.receipt.dispatch_phase || batch.receipt.state}. This does not mean the agent consumed it.`,
      ),
      link(
        "Open the actual session",
        sessionLink(partition.identity.server, batch.receipt.session_id),
      ),
    );
  if (batch.error) {
    const error = node("p", batch.error);
    error.className = "error";
    card.append(error);
  }
  const controls = node("div");
  controls.className = "row";
  const ready = view!.auth.status === "connected";
  const busy = view!.busy.includes(batch.id);
  if (!old && batch.state !== "moved") {
    const bound = { key: partition.key, batchId: batch.id, targetId: batch.target.id };
    controls.append(
      action(
        "Check delivery",
        () => {
          void command({ type: "check", ...bound });
        },
        !ready || busy,
      ),
    );
    if (!batch.receipt && (["ready", "error"].includes(batch.state) || batch.notRecorded))
      controls.append(
        action(
          "Retry same batch and target",
          () => {
            void command({ type: "retry", ...bound });
          },
          !ready || busy,
        ),
      );
    if (batch.receipt?.dispatch_phase === "ready" && batch.receipt.retry_required)
      controls.append(
        action(
          "Retry first dispatch",
          () => {
            void command({ type: "retryDelivery", ...bound });
          },
          !ready || busy,
        ),
      );
  }
  if (old && batch.items.length) {
    const phrase = batch.attempted
      ? `NOT ADMITTED ${partition.identity.grantId} ${batch.id}`
      : `MOVE ${partition.identity.grantId}`;
    controls.append(
      action(
        batch.attempted ? "I checked: not admitted — move draft" : "Move never-attempted draft",
        () =>
          confirmCommand(
            "Use only the signed-in delivery check's exact not-admitted result after the old grant is closed. Unknown does not qualify.",
            phrase,
            { ...context(), type: "recover", oldKey: partition.key, batchId: batch.id },
          ),
        !ready || (batch.attempted && (!partition.closed || !!batch.receipt)),
      ),
    );
  }
  controls.append(
    action(
      "Send another copy (possible duplicate)",
      () =>
        confirmCommand(
          "Fresh batch/capture IDs will be created in the current draft. This may duplicate an admitted message.",
          `COPY ${batch.id}`,
          { ...context(), type: "copy", oldKey: partition.key, batchId: batch.id },
        ),
      !ready || !batch.items.length || busy,
    ),
  );
  if (!batch.items.length)
    card.append(
      node(
        "small",
        "Local screenshot copies are unavailable. Capture new evidence to send another copy.",
      ),
    );
  controls.append(
    action(
      "Discard local record",
      () =>
        confirmCommand(
          "Discard local evidence/receipt only. This cannot unsend a message.",
          `DISCARD ${batch.id}`,
          { type: "discard", oldKey: partition.key, batchId: batch.id },
        ),
      !ready || busy,
    ),
  );
  card.append(controls);
  if (batch.items.length) {
    const details = node("details");
    details.append(node("summary", `${batch.items.length} frozen notes`));
    for (const item of batch.items) details.append(itemCard(item, partition, false));
    card.append(details);
  }
  return card;
}
function render(next: AppView): void {
  if (
    view?.current?.key === next.current?.key &&
    (view?.current?.revision ?? 0) > (next.current?.revision ?? 0)
  )
    return;
  view = next;
  const active = document.activeElement as HTMLTextAreaElement | null;
  const focusKey = active?.dataset.noteKey;
  const selection = [active?.selectionStart, active?.selectionEnd];
  const connected = next.auth.status === "connected";
  byId("connect-form").hidden = next.auth.status !== "disconnected";
  const connection = byId("connection");
  connection.replaceChildren();
  if (next.auth.identity)
    connection.append(
      node("strong", next.auth.identity.display),
      node("p", next.auth.identity.server),
      node("p", `Connection: ${next.auth.status}`),
    );
  if (next.auth.code) {
    connection.append(
      node("p", `Verify this code in Omnigent: ${next.auth.code}`),
      node("p", `Browser: ${next.auth.label}`),
      link("Open signed-in pairing page", next.auth.consentUrl!),
      action("Check pairing approval", () => {
        void command({ type: "poll" });
      }),
    );
    clearTimeout(pollTimer);
    pollTimer = setTimeout(
      () => {
        void command({ type: "poll" });
      },
      Math.max(1000, (next.auth.pollAfter ?? Date.now() + 5000) - Date.now()),
    );
  } else clearTimeout(pollTimer);
  if (next.auth.status !== "disconnected") {
    const disconnect = (mode: string) =>
      confirmCommand(
        "Choose Keep to retain unsent notes. Local disconnect cannot confirm server revocation; revoke it in Omnigent Connected apps.",
        `DISCONNECT ${mode}`,
        {
          type: "disconnect",
          mode,
          key: next.auth.identity ? identityKey(next.auth.identity) : "",
        },
      );
    connection.append(
      action("Disconnect — revoke and keep notes", () => disconnect("revoke_keep")),
      action("Disconnect locally — keep notes", () => disconnect("local_keep")),
      action("Revoke and delete this grant's local data", () => disconnect("revoke_delete")),
    );
    if (next.auth.server)
      connection.append(
        link("Open Connected apps / delivery check", deliveryCheckLink(next.auth.server)),
      );
  }
  byId("target-section").hidden = !next.current || !connected;
  if (next.current) renderTargets(next.current);
  const tray = byId("tray");
  tray.replaceChildren();
  const current = next.current;
  const full = byId<HTMLInputElement>("full-url");
  full.checked = current?.includeFullURL ?? false;
  full.disabled = !connected;
  if (current) for (const item of current.items) tray.append(itemCard(item, current, connected));
  if (!current?.items.length)
    tray.append(
      node(
        "p",
        "Pick an element, region or selected text to add evidence. New captures stay separate from frozen batches.",
      ),
    );
  byId("size").textContent = current
    ? `· ${(current.bytes / 1024 / 1024).toFixed(2)} MiB / 20 MiB`
    : "";
  const send = byId<HTMLButtonElement>("send");
  send.disabled =
    !connected ||
    !current?.items.length ||
    !current.target?.available ||
    current.items.some(
      (item) =>
        !item.wire.note.trim() ||
        item.choice === "pending" ||
        notes.has(`${current.key}:${item.wire.capture_id}`),
    );
  send.textContent = current?.target
    ? `Send ${current.items.length} annotations to ${targetLabel(current.target)}`
    : "Choose a target and add notes";
  const batches = byId("batches");
  batches.replaceChildren();
  if (current)
    for (const batch of current.batches) batches.append(batchCard(current, batch, false));
  const recovery = byId("recovery");
  recovery.replaceChildren();
  for (const old of next.old) {
    const section = node("section");
    section.className = "card";
    section.append(
      node("h3", `Previous grant ${old.identity.grantId}`),
      node(
        "p",
        old.closed
          ? "Grant closed (recorded confirmation)."
          : "Grant closure unconfirmed. Close it in signed-in Omnigent before migrating an attempted batch.",
      ),
      link("Open Omnigent delivery check", deliveryCheckLink(old.identity.server)),
    );
    if (!old.closed)
      section.append(
        action(
          "I closed this grant in Omnigent",
          () =>
            confirmCommand(
              "Verify this exact grant was revoked/expired in the signed-in first-party view. Page messages are not accepted as confirmation.",
              `CLOSED ${old.identity.grantId}`,
              { type: "closed", oldKey: old.key },
            ),
          !connected,
        ),
      );
    if (old.items.length) {
      section.append(
        action(
          "Move never-attempted notes to this connection",
          () =>
            confirmCommand(
              "Move these drafts with fresh capture IDs. This does not send them.",
              `MOVE ${old.identity.grantId}`,
              { ...context(), type: "recover", oldKey: old.key, batchId: "draft" },
            ),
          !connected,
        ),
      );
      for (const item of old.items) section.append(itemCard(item, old, false));
    }
    for (const batch of old.batches) section.append(batchCard(old, batch, true));
    recovery.append(section);
  }
  if (focusKey) {
    const replacement = Array.from(document.querySelectorAll<HTMLTextAreaElement>("textarea")).find(
      (note) => note.dataset.noteKey === focusKey,
    );
    replacement?.focus({ preventScroll: true });
    if (replacement && selection[0] != null && selection[1] != null)
      replacement.setSelectionRange(selection[0], selection[1]);
  }
  if (next.message) status.textContent = next.message;
}
byId("connect-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!event.isTrusted) return;
  try {
    const server = canonicalServer(byId<HTMLInputElement>("server").value);
    const label = byId<HTMLInputElement>("label").value;
    // Keep this permission request in the trusted gesture, before await or a worker hop.
    const requested = chrome.permissions.request({ origins: [originPermission(server)] });
    void requested
      .then((granted) => {
        if (granted) return command({ type: "connect", server, label });
        status.textContent = "Server permission declined. Nothing connected.";
        return false;
      })
      .catch(() => {
        status.textContent = "Server permission could not be requested.";
      });
  } catch (error) {
    status.textContent = error instanceof Error ? error.message : "Invalid server URL.";
  }
});
byId("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (event.isTrusted)
    void command({ type: "list", query: byId<HTMLInputElement>("search").value, cursor: "" });
});
byId("more").addEventListener("click", (event) => {
  if (event.isTrusted)
    void command({
      type: "list",
      query: byId<HTMLInputElement>("search").value,
      cursor: view?.targets.next_cursor ?? "",
    });
});
byId("refresh").addEventListener("click", (event) => {
  if (event.isTrusted)
    void command({ type: "list", query: byId<HTMLInputElement>("search").value, cursor: "" });
});
byId("send").addEventListener("click", (event) => {
  if (event.isTrusted && !(event.currentTarget as HTMLButtonElement).disabled)
    void command({ ...context(), type: "send" });
});
byId("save-preview").addEventListener("click", (event) => {
  if (event.isTrusted && view?.current)
    void command({ type: "savePreview", key: view.current.key });
});
byId("full-url").addEventListener("change", (event) => {
  if (event.isTrusted)
    void command({
      ...context(),
      type: "fullUrl",
      enabled: byId<HTMLInputElement>("full-url").checked,
    });
});
byId("copy").addEventListener("click", (event) => {
  if (!event.isTrusted) return;
  const items = view?.current?.items ?? [];
  void navigator.clipboard
    .writeText(
      items
        .map(
          (item) =>
            `${notes.get(`${view?.current?.key}:${item.wire.capture_id}`) ?? item.wire.note}\n\n${evidence(item)}\n(Screenshots are not included in text copy.)`,
        )
        .join("\n\n---\n\n"),
    )
    .catch(() => {
      status.textContent = "Copy failed. Select and copy the visible text manually.";
    });
});
if (/Mac/.test(navigator.platform)) byId("shortcut").textContent = "Cmd+Shift+Y";
const port = chrome.runtime.connect({ name: "annotation-preview" });
port.onMessage.addListener((input) => {
  const state = input as PanelState;
  if (state.app) render(state.app);
  if (state.message) status.textContent = state.message;
  const capture = state.preview;
  byId("preview").hidden = !capture;
  if (!capture) return;
  const picture = byId<HTMLImageElement>("image");
  picture.hidden = !capture.image;
  if (capture.image?.startsWith("data:image/png;base64,")) picture.src = capture.image;
  else picture.removeAttribute("src");
  byId("failure").hidden = !capture.screenshotFailure;
  byId("failure").textContent = capture.screenshotFailure ?? "";
  byId("evidence").textContent = JSON.stringify(
    {
      kind: capture.selection.kind,
      page: capture.selection.page,
      target: capture.selection.target,
    },
    null,
    2,
  );
});
port.onDisconnect.addListener(() => {
  clearTimeout(pollTimer);
  status.textContent =
    "Extension disconnected. Close and reopen the panel. Typed notes remain here until you close it.";
});
