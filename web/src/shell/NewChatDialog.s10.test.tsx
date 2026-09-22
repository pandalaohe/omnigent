// MOD-s10 fork tests for the landing composer: attachments live as visible
// `[image N]` / `[file N]` tokens in upstream's textarea, and the create path
// hands their order to ChatPage. Mounted the way the neighbouring landing
// tests mount it (NewChatDialog.flow.test.tsx), with the same minimum stubs.

import type * as SandboxModelOptionsModule from "@/hooks/useSandboxModelOptions";

vi.mock("@/hooks/useSandboxModelOptions", async (importOriginal) => ({
  ...(await importOriginal<typeof SandboxModelOptionsModule>()),
  useSandboxModelOptions: vi.fn(() => ({
    data: {
      configured: false,
      status: "unconfigured",
      models: [],
      configuration_revision: null,
      provider_label: null,
      default_model: null,
    },
    isLoading: false,
    error: null,
  })),
}));
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useConversations as useTestConversations } from "@/hooks/useConversations";

vi.mock("@/hooks/useSidebarData", () => ({ useLoadedConversations: () => useTestConversations() }));

vi.mock("@/hooks/useSkills", () => ({
  useSkills: ({ target, enabled }: { target: { agentId?: string } | null; enabled?: boolean }) => ({
    skills:
      useAvailableAgents().data?.find((candidate) => candidate.id === target?.agentId)?.skills ??
      [],
    skillsStatus: enabled === false || target === null ? "unavailable" : "ready",
    refetch: vi.fn(),
  }),
}));
import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as AgentLabelsModule from "@/lib/agentLabels";
import type * as CustomAgentsApiModule from "@/lib/customAgentsApi";
import type { SessionListWireItem } from "@/lib/sessionListCache";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";

import { authenticatedFetch } from "@/lib/identity";
import type { Host } from "@/hooks/useHosts";
import { useHostModelOptions, useHosts } from "@/hooks/useHosts";
import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useCustomAgents } from "@/lib/customAgentsApi";
import { NewChatLandingScreen, resetLandingDraft } from "./NewChatDialog";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type { ServerInfo } from "@/lib/capabilities";
import { ImageLightboxProvider } from "@/components/ImageLightbox";
import { TooltipProvider } from "@/components/ui/tooltip";
import { clearSessionDrafts, hasSessionDraft, setSessionDraft } from "@/lib/sessionDrafts";
import { assignLabels } from "@/lib/composerTokens";

const navigateMock = vi.fn();
const setPendingInitialPromptMock = vi.fn();
const beginLocalConversationMock = vi.fn();
const hydrateLocalConversationMock = vi.fn();
let searchParams = new URLSearchParams();
let projects: { id: string | null; name: string }[] = [];

const RECENT_KEY = "omnigent:recent-workspaces";
const SEEDED_WORKSPACE = "/Users/corey/universe/src/foo";
const TEMP_ID = "temp:1234567890abcdef1234567890abcdef";

vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigateMock,
  useSearchParams: () => [searchParams, vi.fn()],
}));

vi.mock("@/store/chatStore", () => ({
  beginLocalConversation: (...args: unknown[]) => beginLocalConversationMock(...args),
  hydrateLocalConversation: (...args: unknown[]) => hydrateLocalConversationMock(...args),
  removeLocalConversation: vi.fn(),
  setPendingInitialPrompt: (...args: unknown[]) => setPendingInitialPromptMock(...args),
}));

vi.mock("@/lib/sessionUpdatesSocket", () => ({
  nextPushedSession: (match: (item: SessionListWireItem) => boolean, signal: AbortSignal) =>
    new Promise<SessionListWireItem | null>((resolve) => {
      signal.addEventListener("abort", () => resolve(null), { once: true });
      void match;
    }),
  sessionUpdatesSocket: {
    subscribeStatus: () => () => {},
    isConnected: () => true,
  },
}));

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
  getCurrentUserId: vi.fn(() => null),
  resolveIdentity: vi.fn(async () => null),
}));
vi.mock("@/lib/customAgentsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof CustomAgentsApiModule>()),
  useCustomAgents: vi.fn(() => ({ data: [], isPending: false, error: null })),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: vi.fn(),
  useHostModelOptions: vi.fn(() => ({
    data: [
      { id: "opus", displayName: "Opus" },
      { id: "sonnet", displayName: "Sonnet" },
      { id: "haiku", displayName: "Haiku" },
    ],
  })),
  useInstallHarness: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useInstallingHarnesses: vi.fn(() => new Set<string>()),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: vi.fn(),
  prefetchAvailableAgentDetails: vi.fn(),
}));
vi.mock("@/hooks/useHostFilesystem", () => ({
  useHostFilesystem: () => ({ data: undefined }),
  useHostFilesystemRoots: () => ({ data: undefined, isLoading: false, error: null }),
  useCreateHostDirectory: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock("@/hooks/useHostWorktrees", () => ({
  useHostWorktrees: () => ({ data: undefined }),
}));
vi.mock("@/hooks/useDirectorySessions", () => ({
  useDirectorySessions: () => ({ data: [] }),
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useRunnerHealthRegistration: () => new Map<string, boolean>(),
}));
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UseConversationsModule>()),
  useProjects: () => ({ data: projects }),
  useProjectConfig: () => ({ data: null, isLoading: false }),
  useConversations: () => ({ data: undefined }),
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({}),
  useHarnessSetupSteps: () => ({}),
}));

function host(overrides: Partial<Host> = {}): Host {
  return {
    host_id: "host_1",
    name: "corey-laptop",
    owner: "corey",
    status: "online",
    ...overrides,
  };
}

function agent(overrides: Partial<AvailableAgent> = {}): AvailableAgent {
  return {
    id: "ag_hello",
    name: "hello_world",
    display_name: "Hello World",
    description: null,
    harness: null,
    skills: [],
    ...overrides,
  };
}

function renderLanding(infoOverrides: Partial<ServerInfo> = {}): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const info = {
    accounts_enabled: false,
    single_user: false,
    login_url: null,
    needs_setup: false,
    databricks_features: false,
    managed_sandboxes_enabled: false,
    sandbox_provider: null,
    enabled_connections: [],
    sharing_mode: "on",
    public_sharing_enabled: true,
    server_version: null,
    smart_routing_enabled: false,
    smart_routing_sources: { external: false, oss: false },
    features: {},
    harness_install_enabled: false,
    installable_harnesses: [],
    dictation_available: false,
    ...infoOverrides,
  } as ServerInfo;
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <CapabilitiesProvider info={info}>
          <TooltipProvider>
            <ImageLightboxProvider>{children}</ImageLightboxProvider>
          </TooltipProvider>
        </CapabilitiesProvider>
      </QueryClientProvider>
    );
  }
  render(<NewChatLandingScreen />, { wrapper: Wrapper });
}

function landingInput(): HTMLTextAreaElement {
  return screen.getByTestId("new-chat-landing-input") as HTMLTextAreaElement;
}

function fileInput(): HTMLInputElement {
  return screen.getByTestId("new-chat-landing-file-input") as HTMLInputElement;
}

function attach(files: File[]): void {
  fireEvent.change(fileInput(), { target: { files } });
}

function pasteFiles(target: HTMLTextAreaElement, files: File[]): void {
  act(() => {
    const event = new Event("paste", { bubbles: true });
    Object.defineProperty(event, "clipboardData", {
      value: { items: files.map((file) => ({ kind: "file", getAsFile: () => file })) },
    });
    target.dispatchEvent(event);
  });
}

function typeMessage(text: string): void {
  fireEvent.change(landingInput(), { target: { value: text } });
}

function img(name = "shot.png"): File {
  return new File([new Uint8Array(10)], name, { type: "image/png" });
}

function pdf(name = "doc.pdf"): File {
  return new File([new Uint8Array(10)], name, { type: "application/pdf" });
}

async function waitForWorkspaceSeed(): Promise<void> {
  await waitFor(() =>
    expect(screen.getByTestId("new-chat-landing-workspace-chip").textContent).toContain("foo"),
  );
}

async function submitCreate(): Promise<void> {
  fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
  await waitFor(() => expect(setPendingInitialPromptMock).toHaveBeenCalled());
}

beforeEach(() => {
  resetLandingDraft();
  clearSessionDrafts();
  localStorage.clear();
  searchParams = new URLSearchParams();
  projects = [];
  vi.mocked(authenticatedFetch).mockReset();
  vi.mocked(useHostModelOptions).mockReturnValue({
    data: [
      { id: "opus", displayName: "Opus" },
      { id: "sonnet", displayName: "Sonnet" },
      { id: "haiku", displayName: "Haiku" },
    ],
    isLoading: false,
  } as unknown as ReturnType<typeof useHostModelOptions>);
  localStorage.setItem(RECENT_KEY, JSON.stringify({ host_1: [SEEDED_WORKSPACE] }));
  vi.mocked(useHosts).mockReturnValue({ data: [host()] } as ReturnType<typeof useHosts>);
  vi.mocked(useAvailableAgents).mockReturnValue({ data: [agent()] } as ReturnType<
    typeof useAvailableAgents
  >);
  vi.mocked(useCustomAgents).mockReturnValue({
    data: [],
    isPending: false,
    error: null,
  } as unknown as ReturnType<typeof useCustomAgents>);
  setPendingInitialPromptMock.mockReset();
  beginLocalConversationMock.mockReset();
  // No client-side temp conversation: the create lands on the server-first
  // handoff unless a test opts into the optimistic path.
  beginLocalConversationMock.mockReturnValue(null);
  hydrateLocalConversationMock.mockReset();
  navigateMock.mockReset();
  vi.mocked(authenticatedFetch).mockResolvedValue({
    ok: true,
    json: async () => ({ id: "conv_new" }),
  } as unknown as Response);
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("NewChatLandingScreen MOD-s10 tokens", () => {
  it("inserts a token at the caret for a picked file", async () => {
    renderLanding();
    const field = landingInput();
    fireEvent.focus(field);
    typeMessage("abcd");
    field.setSelectionRange(2, 2);

    attach([img()]);

    expect(landingInput().value).toBe("ab [image 1] cd");
  });

  it("inserts a token at the caret for a pasted file", () => {
    renderLanding();
    const field = landingInput();
    fireEvent.focus(field);
    typeMessage("abcd");
    field.setSelectionRange(2, 2);

    pasteFiles(field, [img()]);

    expect(landingInput().value).toBe("ab [image 1] cd");
  });

  it("inserts a token for a file dropped on the landing surface", () => {
    renderLanding();
    const field = landingInput();
    fireEvent.focus(field);
    typeMessage("abcd");
    field.setSelectionRange(2, 2);

    fireEvent.drop(screen.getByTestId("new-chat-landing"), {
      dataTransfer: { types: ["Files"], files: [img()] },
    });

    expect(landingInput().value).toBe("ab [image 1] cd");
  });

  it("deletes a token whole with Backspace at its edge and marks the tile", () => {
    renderLanding();
    attach([img("one.png")]);
    typeMessage("[image 1] x");
    landingInput().setSelectionRange("[image 1] ".length, "[image 1] ".length);

    fireEvent.keyDown(landingInput(), { key: "Backspace" });

    expect(landingInput().value).toBe("x");
    expect(screen.getByText(/not in text/)).toBeInTheDocument();
  });

  it("removes the token when its tile is removed", () => {
    renderLanding();
    attach([img("one.png")]);
    attach([pdf("notes.pdf")]);
    expect(landingInput().value).toBe("[image 1] [file 1] ");

    fireEvent.click(screen.getByRole("button", { name: "Remove one.png" }));

    expect(landingInput().value).toBe("[file 1] ");
  });

  it("hands the create the tokens' order and the token-free text", async () => {
    renderLanding();
    await waitForWorkspaceSeed();
    const first = img("one.png");
    const second = img("two.png");

    attach([first]);
    typeMessage("[image 1] between");
    attach([second]);
    expect(landingInput().value).toBe("[image 1] between [image 2] ");

    await submitCreate();

    expect(setPendingInitialPromptMock).toHaveBeenCalledWith(
      "conv_new",
      expect.objectContaining({
        text: " between ",
        files: [first, second],
        composerParts: [
          { type: "attachment", file: first },
          { type: "text", text: " between " },
          { type: "attachment", file: second },
        ],
      }),
    );
  });

  it("T16 — a recovery collision relabels the second file's token", async () => {
    const first = img("one.png");
    renderLanding();
    await waitForWorkspaceSeed();
    attach([first]);
    typeMessage("first [image 1]");
    // The temp session's own draft, carrying a file that also took [image 1].
    const second = img("two.png");
    assignLabels([second], []);
    setSessionDraft(TEMP_ID, { text: "[image 1] second", files: [second] });
    beginLocalConversationMock.mockReturnValue({
      tempConvId: TEMP_ID,
      pendingMsgTempId: "pend_1",
      createToken: "1234567890abcdef1234567890abcdef",
    });
    vi.mocked(authenticatedFetch).mockRejectedValue(new Error("offline"));

    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));

    await waitFor(() => expect(landingInput().value).toBe("first [image 1]\n\n[image 2] second"));
    expect(screen.getByRole("button", { name: "[image 2]" })).toBeInTheDocument();
  });

  it("F2 — an off-screen create failure reconciles the saved draft's labels too", async () => {
    const first = img("one.png");
    renderLanding();
    await waitForWorkspaceSeed();
    attach([first]);
    typeMessage("first [image 1]");
    const second = img("two.png");
    assignLabels([second], []);
    setSessionDraft(TEMP_ID, { text: "[image 1] second", files: [second] });
    beginLocalConversationMock.mockReturnValue({
      tempConvId: TEMP_ID,
      pendingMsgTempId: "pend_1",
      createToken: "1234567890abcdef1234567890abcdef",
    });
    vi.mocked(authenticatedFetch).mockRejectedValue(new Error("offline"));

    fireEvent.click(screen.getByTestId("new-chat-landing-submit"));
    // The create outlives the landing screen: the user navigated away.
    cleanup();
    await waitFor(() => expect(hasSessionDraft(TEMP_ID)).toBe(false));

    renderLanding();
    await waitFor(() => expect(landingInput().value).toBe("first [image 1]\n\n[image 2] second"));
    expect(screen.getByRole("button", { name: "[image 2]" })).toBeInTheDocument();
  });

  it("T19 — plain prose mounts no backdrop", () => {
    renderLanding();
    typeMessage("just a normal message");

    expect(screen.queryByTestId("composer-highlight-overlay")).toBeNull();
    expect(landingInput().className).not.toContain("text-transparent");
  });

  it("T20 — a command plus a token mounts the backdrop and tints both", () => {
    renderLanding();
    attach([img()]);
    typeMessage("/review [image 1] ");

    const overlay = screen.getByTestId("composer-highlight-overlay");
    expect(overlay.querySelector(".text-brand-accent")?.textContent).toBe("/review");
    expect(overlay.querySelector(".text-primary")?.textContent).toBe("[image 1]");
    expect(landingInput()).toHaveClass("text-transparent");
  });

  it("T23 — the badge moves the caret; the image keeps opening the lightbox", () => {
    window.URL.createObjectURL = vi.fn(() => "blob:mock");
    window.URL.revokeObjectURL = vi.fn();
    renderLanding();
    attach([img("one.png")]);
    typeMessage("hi [image 1] there");

    fireEvent.click(screen.getByRole("button", { name: "[image 1]" }));
    const field = landingInput();
    expect(document.activeElement).toBe(field);
    expect(field.selectionStart).toBe("hi [image 1]".length);

    fireEvent.click(screen.getByRole("button", { name: "Zoom image: one.png" }));
    expect(screen.getByLabelText("Zoom in")).toBeInTheDocument();
  });

  it("T25 — a rejected file inserts no token", () => {
    renderLanding();
    const rejected = new File([new Uint8Array(10)], "clip.mp4", { type: "video/mp4" });

    attach([rejected]);

    expect(landingInput().value).toBe("");
    expect(screen.getByText(/can't be attached/)).toBeInTheDocument();
  });
});
