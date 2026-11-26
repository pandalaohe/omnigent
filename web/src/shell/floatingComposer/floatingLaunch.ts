// How the floating New Agent panel hands off a session it created: open it in a main window,
// then send the first message unless the create already carried it.
import { floatingComposerBridge } from "@/lib/nativeBridge";
import { randomUUID } from "@/lib/randomUUID";
import { isMessageItem } from "@/lib/conversationItems";
import { provesNotDelivered } from "@/lib/sendRefusal";
import { fetchSessionItemsPage, postEvent } from "@/lib/sessionsApi";
import { postDeliveredInput, sendVerdictFromServer } from "@/lib/userInputDelivery";
import type { FloatingLaunchRequest } from "@/shell/composerPresentation";
import { uploadComposerParts, uploadFileBlocks } from "@/store/chatStore";

/** The panel's next state: an error to show, and whether to start a fresh composer. */
export interface LaunchOutcome {
  error: string | null;
  freshComposer: boolean;
}

type Delivery = "delivered" | "failed" | "unknown";

export const SEND_FAILED_ERROR =
  "The new agent didn't get your message. Your prompt is still here; its session is in the sidebar.";
export const SEND_UNKNOWN_ERROR =
  "Couldn't confirm the new agent got your message. Check its session before sending it again.";
export const OPEN_FAILED_ERROR =
  "Your agent started, but Omnigent couldn't show it. Find it in the sidebar.";

/**
 * Open the launched session in a main window, then deliver its first message. Never rejects:
 * the session already exists, so every failure becomes the panel's error.
 */
export async function launchFromPanel(request: FloatingLaunchRequest): Promise<LaunchOutcome> {
  const opened = await Promise.resolve()
    .then(() => floatingComposerBridge()?.openInMainWindow(`/c/${request.sessionId}`))
    .catch(() => null);
  const delivery: Delivery = request.inputAccepted
    ? "delivered"
    : await deliverFirstMessage(request).catch(() => "unknown" as const);
  if (delivery === "failed") return { error: SEND_FAILED_ERROR, freshComposer: false };
  if (delivery === "unknown") return { error: SEND_UNKNOWN_ERROR, freshComposer: false };
  return { error: opened?.ok ? null : OPEN_FAILED_ERROR, freshComposer: true };
}

async function deliverFirstMessage({
  sessionId,
  text,
  skill,
  files,
  composerParts,
}: FloatingLaunchRequest): Promise<Delivery> {
  const stableId = randomUUID().replace(/-/g, "");
  let content;
  try {
    content = skill
      ? []
      : composerParts
        ? await uploadComposerParts(sessionId, composerParts)
        : [
            ...(await uploadFileBlocks(sessionId, files ?? [])),
            ...(text.trim() ? [{ type: "input_text" as const, text }] : []),
          ];
  } catch {
    return "failed"; // Nothing was posted.
  }
  try {
    // No `holdWhenHostOffline`: the panel can't resend later, so an offline host's failure
    // is recorded in the session, where its window shows it.
    const response = await postEvent(
      sessionId,
      skill
        ? {
            type: "slash_command",
            data: { kind: "skill", name: skill.name, arguments: skill.args, stable_id: stableId },
          }
        : {
            type: "message",
            data: {
              role: "user",
              content,
              stable_id: stableId,
            },
          },
    );
    return postDeliveredInput(response) ? "delivered" : "failed";
  } catch (error) {
    if (provesNotDelivered(error)) return "failed";
    return deliveryFromServer(sessionId, stableId);
  }
}

/**
 * What became of a message whose post got no clear answer. The server can store our message
 * before forwarding it. A native transcript's own correlation to this send is receipt evidence.
 */
async function deliveryFromServer(sessionId: string, stableId: string): Promise<Delivery> {
  const verdict = await sendVerdictFromServer(sessionId, stableId);
  if (verdict === "refused") return "failed";
  const page = await fetchSessionItemsPage(sessionId).catch(() => null);
  const mirrored = page?.items.some(
    (item) =>
      isMessageItem(item) &&
      item.role === "user" &&
      item.status !== "failed" &&
      item.web_stable_id === stableId,
  );
  return mirrored ? "delivered" : "unknown";
}
