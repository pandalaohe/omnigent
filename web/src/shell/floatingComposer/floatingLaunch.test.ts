import { beforeEach, describe, expect, it, vi } from "vitest";
import type { MessageItem } from "@/lib/conversationItems";
import type * as SessionsApi from "@/lib/sessionsApi";
import type * as UserInputDelivery from "@/lib/userInputDelivery";

const bridge = vi.hoisted(() => ({
  openInMainWindow: vi.fn(),
  dismiss: vi.fn(),
  filePicker: vi.fn(),
  announceSupport: vi.fn(),
}));
const bridgeOf = vi.hoisted(() => vi.fn());
vi.mock("@/lib/nativeBridge", () => ({ floatingComposerBridge: bridgeOf }));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApi>()),
  postEvent: vi.fn(),
  fetchSessionItemsPage: vi.fn(),
}));
vi.mock("@/lib/userInputDelivery", async (importOriginal) => ({
  ...(await importOriginal<typeof UserInputDelivery>()),
  sendVerdictFromServer: vi.fn(),
}));
vi.mock("@/store/chatStore", () => ({ uploadFileBlocks: vi.fn(), uploadComposerParts: vi.fn() }));

import { ApiError, fetchSessionItemsPage, postEvent } from "@/lib/sessionsApi";
import { sendVerdictFromServer } from "@/lib/userInputDelivery";
import { uploadComposerParts, uploadFileBlocks } from "@/store/chatStore";
import type { FloatingLaunchRequest } from "@/shell/composerPresentation";
import {
  launchFromPanel,
  OPEN_FAILED_ERROR,
  SEND_FAILED_ERROR,
  SEND_UNKNOWN_ERROR,
} from "./floatingLaunch";

const request = (overrides: Partial<FloatingLaunchRequest> = {}): FloatingLaunchRequest => ({
  sessionId: "conv_1",
  text: "Fix the build",
  skill: null,
  inputAccepted: false,
  ...overrides,
});

/** The stable id the launch posted its message under. */
const postedStableId = () =>
  (vi.mocked(postEvent).mock.calls[0]![1].data as { stable_id: string }).stable_id;

const delivered = { error: null, freshComposer: true };
const failed = { error: SEND_FAILED_ERROR, freshComposer: false };
const unknown = { error: SEND_UNKNOWN_ERROR, freshComposer: false };

const historyMessage = (id: string, webStableId?: string): MessageItem => ({
  id,
  type: "message",
  response_id: "resp_1",
  status: "completed",
  role: "user",
  content: [{ type: "input_text", text: "Fix the build" }],
  ...(webStableId ? { web_stable_id: webStableId } : {}),
});

beforeEach(() => {
  vi.clearAllMocks();
  bridgeOf.mockReturnValue(bridge);
  bridge.openInMainWindow.mockResolvedValue({ ok: true });
  vi.mocked(uploadFileBlocks).mockResolvedValue([]);
  vi.mocked(fetchSessionItemsPage).mockResolvedValue({ items: [], hasMore: false });
  vi.mocked(sendVerdictFromServer).mockResolvedValue(null);
  vi.mocked(postEvent).mockResolvedValue({ queued: false } as Awaited<
    ReturnType<typeof postEvent>
  >);
});

describe("launchFromPanel", () => {
  it("opens the session in a main window, then posts the message once with its files", async () => {
    const file = new File(["x"], "notes.txt", { type: "text/plain" });
    vi.mocked(uploadFileBlocks).mockResolvedValue([
      { type: "input_file", file_id: "file_1", filename: "notes.txt" },
    ]);

    const outcome = await launchFromPanel(request({ files: [file] }));

    expect(bridge.openInMainWindow).toHaveBeenCalledWith("/c/conv_1");
    expect(uploadFileBlocks).toHaveBeenCalledWith("conv_1", [file]);
    expect(postEvent).toHaveBeenCalledTimes(1);
    // No hold for an offline host: the panel can't resend later.
    expect(postEvent).toHaveBeenCalledWith("conv_1", {
      type: "message",
      data: {
        role: "user",
        content: [
          { type: "input_file", file_id: "file_1", filename: "notes.txt" },
          { type: "input_text", text: "Fix the build" },
        ],
        stable_id: postedStableId(),
      },
    });
    expect(outcome).toEqual(delivered);
  });

  it("posts attachments in the order authored by the floating composer", async () => {
    const composerParts = [
      { type: "text", text: "Before" },
      { type: "attachment", file: new File(["x"], "notes.txt", { type: "text/plain" }) },
      { type: "text", text: "After" },
    ] as const;
    const content = [
      { type: "input_text", text: "Before" },
      { type: "input_file", file_id: "file_1", filename: "notes.txt" },
      { type: "input_text", text: "After" },
    ] as const;
    vi.mocked(uploadComposerParts).mockResolvedValue([...content]);

    expect(await launchFromPanel(request({ composerParts: [...composerParts] }))).toEqual(
      delivered,
    );

    expect(uploadComposerParts).toHaveBeenCalledWith("conv_1", composerParts);
    expect(uploadFileBlocks).not.toHaveBeenCalled();
    expect(postEvent).toHaveBeenCalledWith(
      "conv_1",
      expect.objectContaining({ data: expect.objectContaining({ content }) }),
    );
  });

  it("posts nothing when the create already carried the message", async () => {
    const outcome = await launchFromPanel(request({ inputAccepted: true }));

    expect(bridge.openInMainWindow).toHaveBeenCalledWith("/c/conv_1");
    expect(postEvent).not.toHaveBeenCalled();
    expect(outcome).toEqual(delivered);
  });

  it("sends a bundled skill as a slash command", async () => {
    await launchFromPanel(request({ text: "/review 12", skill: { name: "review", args: "12" } }));

    expect(uploadFileBlocks).not.toHaveBeenCalled();
    expect(postEvent).toHaveBeenCalledWith("conv_1", {
      type: "slash_command",
      data: { kind: "skill", name: "review", arguments: "12", stable_id: postedStableId() },
    });
  });

  it("keeps the prompt when the server answers with a refusal", async () => {
    vi.mocked(postEvent).mockRejectedValue(
      new ApiError("The host is offline", 503, "host_offline"),
    );

    expect(await launchFromPanel(request())).toEqual(failed);
    expect(sendVerdictFromServer).not.toHaveBeenCalled();
  });

  it("keeps the prompt when the server persisted it without forwarding", async () => {
    vi.mocked(postEvent).mockResolvedValue({ queued: true, forwarded: false });

    expect(await launchFromPanel(request())).toEqual(failed);
    expect(sendVerdictFromServer).not.toHaveBeenCalled();
  });

  it("checks the same request ID when a coded error can follow delivery", async () => {
    vi.mocked(postEvent).mockRejectedValue(
      new ApiError("Runner unavailable", 503, "runner_unavailable"),
    );
    vi.mocked(sendVerdictFromServer).mockResolvedValue("delivered");
    vi.mocked(fetchSessionItemsPage).mockImplementation(async () => ({
      items: [historyMessage("native_item", postedStableId())],
      hasMore: false,
    }));

    expect(await launchFromPanel(request())).toEqual(delivered);
    expect(sendVerdictFromServer).toHaveBeenCalledWith("conv_1", postedStableId());
  });

  it("keeps the draft when a coded error only left its original item in history", async () => {
    vi.mocked(postEvent).mockRejectedValue(
      new ApiError("Runner unavailable", 503, "runner_unavailable"),
    );
    vi.mocked(sendVerdictFromServer).mockResolvedValue("delivered");
    vi.mocked(fetchSessionItemsPage).mockImplementation(async () => ({
      items: [historyMessage(postedStableId())],
      hasMore: false,
    }));
    expect(await launchFromPanel(request())).toEqual(unknown);
  });

  it("keeps the prompt when a policy denies the message", async () => {
    vi.mocked(postEvent).mockResolvedValue({ queued: false, denied: true } as Awaited<
      ReturnType<typeof postEvent>
    >);

    expect(await launchFromPanel(request())).toEqual(failed);
  });

  it("keeps the prompt without posting when an attachment can't upload", async () => {
    vi.mocked(uploadFileBlocks).mockRejectedValue(new TypeError("Failed to fetch"));

    expect(await launchFromPanel(request({ files: [new File(["x"], "a.txt")] }))).toEqual(failed);
    expect(postEvent).not.toHaveBeenCalled();
  });

  describe("with no answer to the post, asks the server", () => {
    beforeEach(() => {
      vi.mocked(postEvent).mockRejectedValue(new TypeError("Failed to fetch"));
      vi.mocked(sendVerdictFromServer).mockResolvedValue("delivered");
    });

    const withItems = (items: (stableId: string) => MessageItem[], hasMore = false) =>
      vi.mocked(fetchSessionItemsPage).mockImplementation(async () => ({
        items: items(postedStableId()),
        hasMore,
      }));

    it("and starts fresh once the mirror names the original send", async () => {
      withItems((stableId) => [historyMessage("native_item", stableId)]);

      expect(await launchFromPanel(request())).toEqual(delivered);
      expect(sendVerdictFromServer).toHaveBeenCalledWith("conv_1", postedStableId());
    });

    it("and keeps the prompt when the runner refused it", async () => {
      vi.mocked(sendVerdictFromServer).mockResolvedValue("refused");

      expect(await launchFromPanel(request())).toEqual(failed);
    });

    // A native runner takes the message before its transcript stores it.
    it("and can't tell when history doesn't have the message yet", async () => {
      withItems(() => [historyMessage("other")]);

      expect(await launchFromPanel(request())).toEqual(unknown);
    });

    it("and can't tell from a history page with more before it", async () => {
      withItems(() => [historyMessage("other")], true);

      expect(await launchFromPanel(request())).toEqual(unknown);
    });

    it("and can't tell when the server can't be read either", async () => {
      vi.mocked(sendVerdictFromServer).mockResolvedValue(null);
      vi.mocked(fetchSessionItemsPage).mockRejectedValue(new TypeError("Failed to fetch"));

      expect(await launchFromPanel(request())).toEqual(unknown);
    });

    it("and can't tell when history can't be read", async () => {
      vi.mocked(fetchSessionItemsPage).mockRejectedValue(new TypeError("Failed to fetch"));

      expect(await launchFromPanel(request())).toEqual(unknown);
    });
  });

  it("never rejects: a delivery step that throws comes back as unknown", async () => {
    vi.mocked(postEvent).mockRejectedValue(new TypeError("Failed to fetch"));
    vi.mocked(sendVerdictFromServer).mockRejectedValue(new Error("unexpected"));

    await expect(launchFromPanel(request())).resolves.toEqual(unknown);
  });

  it("still sends the message when the shell throws while opening the session", async () => {
    bridge.openInMainWindow.mockImplementation(() => {
      throw new Error("gone");
    });

    expect(await launchFromPanel(request())).toEqual({
      error: OPEN_FAILED_ERROR,
      freshComposer: true,
    });
    expect(postEvent).toHaveBeenCalledTimes(1);
  });

  it("keeps the prompt when neither the open nor the send worked", async () => {
    bridge.openInMainWindow.mockRejectedValue(new Error("gone"));
    vi.mocked(postEvent).mockResolvedValue({ queued: false, denied: true } as Awaited<
      ReturnType<typeof postEvent>
    >);

    // The failure text points to the sidebar, so it holds whether or not a window opened.
    expect(await launchFromPanel(request())).toEqual(failed);
    expect(SEND_FAILED_ERROR).toMatch(/sidebar/);
  });

  it.each([
    [
      "couldn't be shown",
      () => bridge.openInMainWindow.mockResolvedValue({ ok: false, error: "window-loading" }),
    ],
    ["failed to open", () => bridge.openInMainWindow.mockRejectedValue(new Error("gone"))],
    ["has no shell to open it", () => bridgeOf.mockReturnValue(undefined)],
  ])("still sends the message when the session %s", async (_case, arrange) => {
    arrange();

    const outcome = await launchFromPanel(request());

    expect(postEvent).toHaveBeenCalledTimes(1);
    expect(outcome).toEqual({ error: OPEN_FAILED_ERROR, freshComposer: true });
  });
});
