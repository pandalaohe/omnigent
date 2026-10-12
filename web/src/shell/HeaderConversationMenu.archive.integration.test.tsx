import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import type * as ConversationsModule from "@/hooks/useConversations";
import type * as UnseenConversationsModule from "@/hooks/useUnseenConversations";
import { setOmnigentHostConfig } from "@/lib/host";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { HeaderConversationMenu } from "./HeaderConversationMenu";

const mocks = vi.hoisted(() => ({
  fetch: vi.fn(),
  showToast: vi.fn(),
  showArchiveUndoToast: vi.fn(),
}));

vi.mock("@/components/ui/toast", () => ({ showToast: mocks.showToast }));

// The success confirmation is the Undo pill, which lives on module state plus
// the app-level Toaster and so cannot render inside this harness. Its own
// rendering and Undo wiring are covered by archiveUndoToast.test.tsx; this test
// owns the PATCH contract, so it asserts the pill was fired for this row.
vi.mock("./archiveUndoToast", () => ({
  showArchiveUndoToast: mocks.showArchiveUndoToast,
}));

vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: () => true }));

// Keep the real archive hook and HTTP helper in this component test. Only the
// unrelated menu mutations are replaced so a click exercises the production
// optimistic overlay, rollback, error toast, and PATCH contract together.
vi.mock("@/hooks/useConversations", async (importOriginal) => {
  const actual = await importOriginal<typeof ConversationsModule>();
  return {
    ...actual,
    useProjects: () => ({ data: [] }),
    useTogglePinnedConversation: () => ({ mutate: vi.fn() }),
    useRenameConversation: () => ({ mutate: vi.fn(), isPending: false }),
    useMoveToProject: () => ({ mutate: vi.fn() }),
    useStopAndDeleteConversation: () => ({ mutate: vi.fn(), isPending: false }),
  };
});

vi.mock("@/hooks/useUnseenConversations", async (importOriginal) => {
  const actual = await importOriginal<typeof UnseenConversationsModule>();
  return { ...actual, markConversationUnread: vi.fn() };
});

const CONVERSATION: Conversation = {
  id: "conv-1",
  object: "conversation",
  title: "Quarterly planning",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_100,
  labels: {},
  permission_level: 3,
  git_branch: "feature/quarterly-planning",
  archived: false,
};

const SECOND_CONVERSATION: Conversation = {
  ...CONVERSATION,
  id: "conv-2",
  title: "Release planning",
};

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status >= 200 && status < 300 ? "OK" : "Server Error",
    json: async () => body,
  } as Response;
}

function LocationProbe() {
  return <output data-testid="location-probe">{useLocation().pathname}</output>;
}

function NavigateElsewhere() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate("/settings/general")}>
      Go elsewhere
    </button>
  );
}

interface HarnessProps {
  client: QueryClient;
  conversation?: Conversation;
  showMenu?: boolean;
  worktreeStatus?: boolean;
}

function harness({
  client,
  conversation = CONVERSATION,
  showMenu = true,
  worktreeStatus = false,
}: HarnessProps) {
  return (
    <QueryClientProvider client={client}>
      <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, worktree_status: worktreeStatus }}>
        <MemoryRouter initialEntries={[`/c/${CONVERSATION.id}`]}>
          {showMenu && (
            <HeaderConversationMenu
              conversation={conversation}
              currentProject={null}
              canShare={false}
              canFork={false}
              onShare={() => {}}
              onFork={() => {}}
            />
          )}
          <NavigateElsewhere />
          <LocationProbe />
        </MemoryRouter>
      </CapabilitiesProvider>
    </QueryClientProvider>
  );
}

function createClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

function seedConversation(client: QueryClient, conversation = CONVERSATION) {
  client.setQueryData(["conversations", "", true], {
    pages: [
      {
        data: [conversation],
        first_id: conversation.id,
        last_id: conversation.id,
        has_more: false,
      },
    ],
    pageParams: [undefined],
  });
}

function cachedArchived(client: QueryClient): boolean | undefined {
  const data = client.getQueryData(["conversations", "", true]) as {
    pages: { data: Conversation[] }[];
  };
  return data.pages[0].data[0].archived;
}

function openMenu() {
  fireEvent.pointerDown(screen.getByRole("button", { name: "Conversation actions" }), {
    button: 0,
  });
}

function clickArchive() {
  openMenu();
  fireEvent.click(screen.getByRole("menuitem", { name: "Archive" }));
}

async function settleMutation(client: QueryClient) {
  await waitFor(() => {
    expect(client.getMutationCache().getAll().at(-1)?.state.status).not.toBe("pending");
  });
  const mutation = client.getMutationCache().getAll().at(-1);
  if (mutation?.state.status === "error") throw mutation.state.error;
  await act(async () => {
    await Promise.resolve();
  });
}

beforeEach(() => {
  mocks.fetch.mockReset();
  mocks.showToast.mockReset();
  mocks.showArchiveUndoToast.mockReset();
  vi.stubGlobal("fetch", mocks.fetch);
  setOmnigentHostConfig({
    serverId: "test-server",
    fetcher: (path, init) => fetch(path, init),
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  setOmnigentHostConfig({});
});

describe("HeaderConversationMenu archive integration", () => {
  it("sends the selected one-time tier in PATCH without saving the preference", async () => {
    mocks.fetch.mockImplementation(async (url: string) =>
      url === "/v1/me"
        ? response({
            preferences: {
              settings: {
                worktree_archive: {
                  mode: "green_only",
                  ask_before_archive: true,
                  rebuildable_paths: ["node_modules/"],
                },
              },
            },
          })
        : url.includes("/worktree-status")
          ? response({
              own: {
                state: "clean",
                reason: "clean worktree",
                path: "/opt/work/app/topic",
                branch: "feature/quarterly-planning",
                merged: false,
                merge_target: "main",
                files: [],
              },
              aggregate: { state: "clean", reason: null },
              blockers: [],
              session_count: 1,
            })
          : response({ ...CONVERSATION, archived: true }),
    );
    const client = createClient();
    seedConversation(client);
    render(harness({ client, worktreeStatus: true }));

    clickArchive();
    await screen.findByTestId("archive-worktree-dialog");
    fireEvent.click(screen.getByRole("radio", { name: /Delete green and yellow worktrees/ }));
    expect(screen.getByTestId("archive-worktree-remember")).not.toBeChecked();
    fireEvent.click(screen.getByTestId("archive-worktree-confirm"));

    await waitFor(() =>
      expect(mocks.fetch.mock.calls.some(([url]) => url === "/v1/sessions/conv-1")).toBe(true),
    );
    const [, patch] = mocks.fetch.mock.calls.find(([url]) => url === "/v1/sessions/conv-1") as [
      string,
      RequestInit,
    ];
    expect(JSON.parse(patch.body as string)).toEqual({
      archived: true,
      worktree_archive_mode: "yellow_too",
    });
    expect(
      mocks.fetch.mock.calls.some(([url]) => url === "/v1/me/preferences/worktree_archive"),
    ).toBe(false);
  });

  it("offers one Archive action and sends the ordinary archive PATCH", async () => {
    mocks.fetch.mockResolvedValue(response({ ...CONVERSATION, archived: true }));
    const client = createClient();
    seedConversation(client);
    render(harness({ client }));

    openMenu();
    expect(screen.getAllByRole("menuitem", { name: "Archive" })).toHaveLength(1);
    expect(screen.queryByRole("menuitem", { name: "Archive and keep worktree" })).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "Archive" }));

    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
    const [, init] = mocks.fetch.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ archived: true });
    expect(screen.queryByTestId("archive-worktree-dialog")).not.toBeInTheDocument();
  });

  it("sends the archive PATCH once, waits for success, confirms it, and returns home", async () => {
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    const client = createClient();
    seedConversation(client);
    render(harness({ client }));

    clickArchive();

    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
    expect(screen.getByTestId("location-probe")).toHaveTextContent("/c/conv-1");
    expect(cachedArchived(client)).toBe(true);

    const [url, init] = mocks.fetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv-1");
    expect(init.method).toBe("PATCH");
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({ archived: true });

    // Reopening the menu while the request is pending exposes a disabled row;
    // selecting it cannot dispatch a second PATCH.
    openMenu();
    const pendingAction = screen.getByRole("menuitem", { name: "Archive" });
    expect(pendingAction).toHaveAttribute("data-disabled");
    fireEvent.click(pendingAction);
    expect(mocks.fetch).toHaveBeenCalledOnce();

    pending.resolve(response({ ...CONVERSATION, archived: true, updated_at: 1_700_000_200 }));

    await waitFor(() => {
      expect(screen.getByTestId("location-probe")).toHaveTextContent("/");
      expect(mocks.showArchiveUndoToast).toHaveBeenCalledOnce();
    });
    const [, undoRows] = mocks.showArchiveUndoToast.mock.calls[0] as [unknown, Conversation[]];
    expect(undoRows.map((row) => row.id)).toEqual(["conv-1"]);
  });

  it("stays on the session and restores its cache row when the PATCH fails", async () => {
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    const client = createClient();
    seedConversation(client);
    render(harness({ client }));

    clickArchive();

    await waitFor(() => {
      expect(mocks.fetch).toHaveBeenCalledOnce();
      expect(cachedArchived(client)).toBe(true);
    });
    pending.resolve(response({ error: "nope" }, 500));

    await waitFor(() => {
      expect(cachedArchived(client)).toBe(false);
      expect(mocks.showToast).toHaveBeenCalledWith(
        "Couldn't archive the session — it's back in the sidebar.",
      );
    });
    expect(screen.getByTestId("location-probe")).toHaveTextContent("/c/conv-1");
  });

  it("does not redirect or announce late success after the user navigates elsewhere", async () => {
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    const client = createClient();
    render(harness({ client }));

    clickArchive();
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("button", { name: "Go elsewhere" }));
    expect(screen.getByTestId("location-probe")).toHaveTextContent("/settings/general");

    pending.resolve(response({ ...CONVERSATION, archived: true }));
    await settleMutation(client);

    expect(screen.getByTestId("location-probe")).toHaveTextContent("/settings/general");
    expect(mocks.showToast).not.toHaveBeenCalled();
  });

  it("does not redirect or announce late success after the menu owner unmounts", async () => {
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    const client = createClient();
    const view = render(harness({ client }));

    clickArchive();
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
    view.rerender(harness({ client, showMenu: false }));

    pending.resolve(response({ ...CONVERSATION, archived: true }));
    await settleMutation(client);

    expect(screen.getByTestId("location-probe")).toHaveTextContent("/c/conv-1");
    expect(mocks.showToast).not.toHaveBeenCalled();
  });

  it("does not redirect a newer session when the pending owner changes", async () => {
    const pending = deferred<Response>();
    mocks.fetch.mockReturnValueOnce(pending.promise);
    const client = createClient();
    const view = render(harness({ client }));

    clickArchive();
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledOnce());
    view.rerender(harness({ client, conversation: SECOND_CONVERSATION }));

    pending.resolve(response({ ...CONVERSATION, archived: true }));
    await settleMutation(client);

    expect(screen.getByTestId("location-probe")).toHaveTextContent("/c/conv-1");
    expect(mocks.showToast).not.toHaveBeenCalled();
  });
});
