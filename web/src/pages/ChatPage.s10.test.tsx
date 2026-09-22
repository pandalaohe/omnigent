// MOD-s10 fork tests: attachments live as visible `[image N]` / `[file N]`
// tokens in upstream's textarea, bind to files at send, and survive the
// restore paths. Mounted the way the upstream composer tests mount it
// (ChatPage.composer.test.tsx) so the assertions speak the same DOM.

import { create } from "zustand";
import type { SkillSummary, SkillsStatus } from "@/lib/types";

import type * as UseWorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseHostsModule from "@/hooks/useHosts";
import type * as RunnerHealthProviderModule from "@/hooks/RunnerHealthProvider";
import type * as AgentLabelsModule from "@/lib/agentLabels";
import type * as GoalApiModule from "@/lib/goalApi";
import type * as UseChildSessionsModule from "@/hooks/useChildSessions";

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createRef, type ComponentRef, type ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";
import { useChatStore, type ChatState } from "@/store/chatStore";
import { clearSessionDrafts } from "@/lib/sessionDrafts";
import { ImageLightboxProvider } from "@/components/ImageLightbox";
import { TooltipProvider } from "@/components/ui/tooltip";
import { assignLabels, bindDraft } from "@/lib/composerTokens";
import { composerPartsFromProjection } from "@/lib/composerContent";
import { serializeReplyDraft, type ReplyDraft } from "@/lib/replyDraft";
import { Composer } from "./ChatPage";

vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof UseWorkspaceChangedFilesModule>()),
  useWorkspaceAllFiles: () => ({ data: undefined }),
  useWorkspaceDirectory: () => ({ data: undefined }),
}));
vi.mock("@/hooks/useGithub", () => ({
  useGithubInfo: () => ({ data: undefined }),
}));
vi.mock("@/hooks/useComposerGitStatus", () => ({
  useComposerGitStatus: () => ({
    branch: null,
    branchState: "unknown",
    isWorktree: null,
    worktreePath: null,
    creationBranch: null,
    repoNameWithOwner: null,
    prCount: 0,
    prNumber: null,
    refresh: () => {},
    refreshing: false,
  }),
}));
vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: () => ({ children: [] }),
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: () => ({ session: { hostId: null }, isLoading: false, error: null }),
}));
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof UseHostsModule>()),
  useHosts: () => ({ data: [] }),
}));
vi.mock("@/hooks/RunnerHealthProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof RunnerHealthProviderModule>()),
  useSessionHostOnline: () => undefined,
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({}),
}));
vi.mock("@/lib/goalApi", async (importOriginal) => ({
  ...(await importOriginal<typeof GoalApiModule>()),
  getGoal: vi.fn(),
}));

const skillsFixture = create<{
  skills: SkillSummary[];
  skillsStatus: SkillsStatus | null;
  refetch: ReturnType<typeof vi.fn>;
}>(() => ({ skills: [], skillsStatus: null, refetch: vi.fn() }));

vi.mock("@/hooks/useSkills", () => ({
  useSkills: ({ starting }: { starting: boolean }) => {
    const state = skillsFixture();
    return {
      ...state,
      skillsStatus:
        state.skillsStatus === "unavailable" && starting ? "loading" : state.skillsStatus,
    };
  },
}));

const CONV = "conv_s10";

type ComposerOverrides = Partial<Omit<Parameters<typeof Composer>[0], "onSend">> & {
  onSend?: Mock;
};

function composerProps(overrides: ComposerOverrides = {}) {
  return {
    status: "idle" as const,
    isWorking: false,
    disabled: false,
    onSend: vi.fn(),
    onStop: vi.fn(),
    agents: undefined,
    selectedAgentId: null,
    permissionLevel: null,
    readOnlyReason: null,
    sendDisabledReason: null,
    effortLevels: ["low", "medium", "high"] as const,
    showEffort: true,
    showModels: false,
    modelPickerKind: null,
    codexModelOptions: [],
    showCodexPlanMode: false,
    ...overrides,
  };
}

function setComposerState(patch: Partial<ChatState> & { skills?: SkillSummary[] }) {
  const { skills, ...chat } = patch;
  useChatStore.setState(chat);
  if (skills !== undefined) skillsFixture.setState({ skills });
}

function renderComposer(ui: ReactElement) {
  return render(
    <TooltipProvider>
      <ImageLightboxProvider>{ui}</ImageLightboxProvider>
    </TooltipProvider>,
  );
}

function textarea(): HTMLTextAreaElement {
  return screen.getByLabelText("Message the agent") as HTMLTextAreaElement;
}

function fileInput(): HTMLInputElement {
  const el = document.querySelector('input[type="file"]') as HTMLInputElement | null;
  if (!el) throw new Error("file input not found");
  return el;
}

/** Attach through the hidden picker input, the paperclip's own path. */
function attach(files: File[]) {
  fireEvent.change(fileInput(), { target: { files } });
}

/** Attach through paste, which must land at the caret like the picker does. */
function pasteFiles(target: HTMLTextAreaElement, files: File[]) {
  act(() => {
    const event = new Event("paste", { bubbles: true });
    Object.defineProperty(event, "clipboardData", {
      value: { items: files.map((file) => ({ kind: "file", getAsFile: () => file })) },
    });
    target.dispatchEvent(event);
  });
}

function img(name = "shot.png"): File {
  return new File([new Uint8Array(10)], name, { type: "image/png" });
}

function pdf(name = "doc.pdf"): File {
  return new File([new Uint8Array(10)], name, { type: "application/pdf" });
}

beforeEach(() => {
  clearSessionDrafts();
  skillsFixture.setState({ skills: [], skillsStatus: null, refetch: vi.fn() });
  setComposerState({ conversationId: CONV, sessionHarness: "claude-sdk", blocks: [] });
  URL.createObjectURL = vi.fn(() => "blob:mock");
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  cleanup();
  clearSessionDrafts();
  vi.restoreAllMocks();
});

describe("MOD-s10 attachment tokens", () => {
  it("inserts a token at the caret for a pasted file", () => {
    const file = img();
    renderComposer(<Composer {...composerProps()} />);
    const ta = textarea();
    fireEvent.focus(ta);
    fireEvent.change(ta, { target: { value: "abcd" } });
    ta.setSelectionRange(2, 2);

    pasteFiles(ta, [file]);

    expect(ta.value).toBe("ab [image 1] cd");
  });

  it("inserts a token at the caret for a dropped file", () => {
    const file = img();
    renderComposer(<Composer {...composerProps()} />);
    const ta = textarea();
    fireEvent.focus(ta);
    fireEvent.change(ta, { target: { value: "abcd" } });
    ta.setSelectionRange(2, 2);

    const card = document.querySelector("[data-composer-card]")!;
    fireEvent.drop(card, { dataTransfer: { types: ["Files"], files: [file] } });

    expect(ta.value).toBe("ab [image 1] cd");
  });

  it("inserts a token at the caret for a picked file", () => {
    const file = img();
    renderComposer(<Composer {...composerProps()} />);
    const ta = textarea();
    fireEvent.focus(ta);
    fireEvent.change(ta, { target: { value: "abcd" } });
    ta.setSelectionRange(2, 2);

    attach([file]);

    expect(ta.value).toBe("ab [image 1] cd");
  });

  it("T24 — sends the attachments in token order around the text", () => {
    const first = img("one.png");
    const second = img("two.png");
    const props = composerProps();
    renderComposer(<Composer {...props} />);
    const ta = textarea();

    attach([first]);
    fireEvent.change(ta, { target: { value: "[image 1] between" } });
    attach([second]);
    expect(ta.value).toBe("[image 1] between [image 2] ");

    fireEvent.keyDown(ta, { key: "Enter" });

    expect(props.onSend).toHaveBeenCalledWith(" between ", [first, second], undefined, [
      { type: "attachment", file: first },
      { type: "text", text: " between " },
      { type: "attachment", file: second },
    ]);
  });

  it("R5 — a Backspace at a token's edge deletes the token whole", () => {
    const file = img("one.png");
    const props = composerProps();
    renderComposer(<Composer {...props} />);
    const ta = textarea();

    attach([file]);
    fireEvent.change(ta, { target: { value: "[image 1] x" } });
    ta.setSelectionRange("[image 1] ".length, "[image 1] ".length);
    fireEvent.keyDown(ta, { key: "Backspace" });

    expect(ta.value).toBe("x");
    expect(screen.getByText(/not in text/)).toBeInTheDocument();
  });

  it("T22 — removing a tile removes its token from the draft", () => {
    const first = img("one.png");
    const second = img("two.png");
    renderComposer(<Composer {...composerProps()} />);

    attach([first]);
    attach([second]);
    expect(textarea().value).toBe("[image 1] [image 2] ");

    fireEvent.click(screen.getByRole("button", { name: "Remove one.png" }));

    expect(textarea().value).toBe("[image 2] ");
  });

  it("R5/T6 — an unreferenced tile is marked and still sent at the end", () => {
    const image = img("one.png");
    const doc = pdf("notes.pdf");
    const props = composerProps();
    renderComposer(<Composer {...props} />);
    const ta = textarea();

    attach([image]);
    attach([doc]);
    fireEvent.change(ta, { target: { value: "[image 1]" } });

    expect(screen.getByText(/not in text/)).toBeInTheDocument();

    fireEvent.keyDown(ta, { key: "Enter" });

    expect(props.onSend).toHaveBeenCalledWith("", [image, doc], undefined, [
      { type: "attachment", file: image },
      { type: "attachment", file: doc },
    ]);
  });

  it("T10 — a token moved into a reply quote still binds, in place", () => {
    const file = img("one.png");
    const props = composerProps();
    const ref = createRef<ComponentRef<typeof Composer>>();
    renderComposer(<Composer {...props} ref={ref} />);

    attach([file]);
    expect(textarea().value).toBe("[image 1] ");
    act(() => ref.current?.appendReplyQuote("Quoted answer"));
    expect(screen.getByTestId("composer-reply-quote")).toHaveTextContent("Quoted answer");

    fireEvent.keyDown(textarea(), { key: "Enter" });

    const [text, files, replyDraft, parts] = props.onSend.mock.calls[0]!;
    expect(text).not.toContain("[image 1]");
    expect(files).toEqual([file]);
    expect(replyDraft!.quotes[0]).toEqual({ before: " ", text: "Quoted answer" });
    expect(parts).toEqual([
      { type: "attachment", file },
      { type: "text", text: " \n\n> Quoted answer" },
    ]);
  });

  it("T14 — a failed quoted send restores the quote and the token", async () => {
    const file = img("one.png");
    assignLabels([file], []);
    const original: ReplyDraft = {
      quotes: [{ id: "q1", before: "hi [image 1] ", text: "quoted" }],
      text: "tail",
    };
    const bound = bindDraft(original, [file], serializeReplyDraft);
    useChatStore.setState({
      failedSendDraft: {
        conversationId: CONV,
        text: serializeReplyDraft(bound.snapshot),
        files: [file],
        composerParts: composerPartsFromProjection(bound.projection, bound.files),
        replyDraft: {
          version: 1,
          quotes: bound.snapshot.quotes.map(({ before, text }) => ({ before, text })),
          text: bound.snapshot.text,
        },
      },
    });

    renderComposer(<Composer {...composerProps()} />);

    await waitFor(() =>
      expect(screen.getByLabelText("Reply text before quote 1")).toHaveValue("hi [image 1] "),
    );
    expect(textarea()).toHaveValue("tail");
  });

  it("T15 — a legacy queued row without parts sends through upstream's path", () => {
    const file = img("one.png");
    useChatStore.setState({
      queuedMessages: [{ queueId: "q1", text: "hello", conversationId: CONV, files: [file] }],
    });
    const props = composerProps();
    renderComposer(<Composer {...props} />);

    fireEvent.keyDown(textarea(), { key: "ArrowUp" });
    expect(textarea().value).toBe("hello");
    fireEvent.keyDown(textarea(), { key: "Enter" });

    expect(props.onSend).toHaveBeenCalledWith("hello", [file]);
    expect(props.onSend.mock.calls[0]).toHaveLength(2);
  });

  it("T19 — plain prose mounts no backdrop and leaves the glyphs opaque", () => {
    renderComposer(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: "just a normal message" } });

    expect(screen.queryByTestId("composer-highlight-overlay")).toBeNull();
    expect(textarea().className).not.toContain("text-transparent");
  });

  it("T20 — a command plus a token mounts the backdrop and tints both", () => {
    const file = img("one.png");
    renderComposer(<Composer {...composerProps()} />);

    attach([file]);
    fireEvent.change(textarea(), { target: { value: "/review [image 1] " } });

    const overlay = screen.getByTestId("composer-highlight-overlay");
    expect(overlay.querySelector(".text-brand-accent")?.textContent).toBe("/review");
    expect(overlay.querySelector(".text-primary")?.textContent).toBe("[image 1]");
    expect(textarea()).toHaveClass("text-transparent");
  });

  it("T23 — the badge moves the caret; the image keeps opening the lightbox", () => {
    const file = img("one.png");
    renderComposer(<Composer {...composerProps()} />);

    attach([file]);
    fireEvent.change(textarea(), { target: { value: "hi [image 1] there" } });

    fireEvent.click(screen.getByRole("button", { name: "[image 1]" }));
    const ta = textarea();
    expect(document.activeElement).toBe(ta);
    expect(ta.selectionStart).toBe("hi [image 1]".length);

    fireEvent.click(screen.getByRole("button", { name: "Zoom image: one.png" }));
    expect(screen.getByLabelText("Zoom in")).toBeInTheDocument();
  });

  it("T25 — a rejected file inserts no token", () => {
    const rejected = new File([new Uint8Array(10)], "clip.mp4", { type: "video/mp4" });
    renderComposer(<Composer {...composerProps()} />);

    attach([rejected]);

    expect(textarea().value).toBe("");
    expect(screen.getByText(/can't be attached/)).toBeInTheDocument();
  });
});
