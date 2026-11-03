// One Host's idle-CLI fold. The state it shows is the policy the Runner uses:
// nothing stored for a Host still on the built-in rule, and the built-in values
// only as placeholders. Every valid edit persists at once — there is no Save —
// and an invalid one is refused without a request.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Host, HostCliRelease, HostCliRetentionResponse } from "@/hooks/useHosts";

const legacyResponse = (): HostCliRetentionResponse => ({
  contract_version: 1,
  configured: false,
  revision: 0,
  policy: { version: 1, idle_threshold_minutes: 60, max_idle_clis: null, close_on_archive: true },
  runtime: null,
  application: { status: "legacy", policy_revision: 0, observed_at: null },
});

const appliedResponse = (): HostCliRetentionResponse => ({
  contract_version: 1,
  configured: true,
  revision: 4,
  policy: { version: 1, idle_threshold_minutes: 60, max_idle_clis: 10, close_on_archive: true },
  runtime: {
    configured: true,
    owner: true,
    policy_revision: 4,
    observed_at: 1_900_000_000,
    families: {
      claude: { idle: 8, active: 4, below_threshold: 2, total: 14 },
      codex: { idle: 3, active: 2, below_threshold: 1, total: 6 },
    },
  },
  application: { status: "applied", policy_revision: 4, observed_at: 1_900_000_000 },
});

const unlimitedResponse = (): HostCliRetentionResponse => ({
  ...appliedResponse(),
  policy: { ...appliedResponse().policy, max_idle_clis: null },
});

const mocks = vi.hoisted(() => ({
  response: null as HostCliRetentionResponse | null,
  releases: [] as HostCliRelease[],
  reliability: { isError: false, error: null as unknown },
  replace: vi.fn(),
  resetPolicy: vi.fn(),
  refetch: vi.fn(),
  archiveMode: "never" as "never" | "green_only" | "yellow_too",
  enabled: [] as boolean[],
}));

vi.mock("@/hooks/useHosts", async () => {
  const actual = await vi.importActual("@/hooks/useHosts");
  return {
    ...actual,
    useHostCliRetention: (_hostId: string | null, enabled = true) => {
      mocks.enabled.push(enabled);
      return {
        data: mocks.response ?? undefined,
        isLoading: false,
        isError: mocks.reliability.isError,
        error: mocks.reliability.error,
        refetch: mocks.refetch,
      };
    },
    useHostCliReleases: () => ({
      data: mocks.releases,
      isLoading: false,
      isError: false,
      error: null,
    }),
    useReplaceHostCliRetention: () => ({
      mutate: mocks.replace,
      mutateAsync: mocks.replace,
      isPending: false,
      error: null,
      reset: vi.fn(),
    }),
    useResetHostCliRetention: () => ({
      mutateAsync: mocks.resetPolicy,
      isPending: false,
      error: null,
      reset: vi.fn(),
    }),
  };
});

vi.mock("@/lib/archiveWorktreePreferences", () => ({
  useArchiveWorktreePreference: () => ({
    preference: { data: { mode: mocks.archiveMode } },
    save: { mutate: vi.fn() },
  }),
}));

import { CliRetentionFold } from "./CliRetentionSettings";

const HOST: Host = { host_id: "host-a", name: "Laptop", owner: "alice", status: "online" };

/**
 * Open the fold and let its toggle land: a summary click dispatches `toggle`
 * as a task, so a synchronous query would read the closed state.
 */
async function renderFold() {
  const view = render(
    <MemoryRouter>
      <CliRetentionFold host={HOST} />
    </MemoryRouter>,
  );
  const summary = view.container.querySelector("summary");
  if (summary === null) throw new Error("the fold has no summary");
  const details = summary.parentElement as HTMLDetailsElement;
  fireEvent.click(summary);
  if (!details.open) {
    details.open = true;
    fireEvent(details, new Event("toggle"));
  }
  await waitFor(() => expect(details.open).toBe(true));
  await waitFor(() =>
    expect(screen.getByTestId("cli-retention-host-a")).toHaveAttribute("open", ""),
  );
  return view;
}

const idleInput = () => screen.getByLabelText("Count a CLI as idle after, in minutes");
const limitInput = () => screen.getByLabelText("Keep at most, per family");

beforeEach(() => {
  mocks.response = legacyResponse();
  mocks.releases = [];
  mocks.reliability = { isError: false, error: null };
  mocks.replace.mockReset();
  mocks.resetPolicy.mockReset();
  mocks.refetch.mockReset();
  mocks.archiveMode = "never";
  mocks.enabled = [];
  mocks.replace.mockImplementation(() => undefined);
  mocks.resetPolicy.mockImplementation(async () => legacyResponse());
  mocks.refetch.mockImplementation(async () => ({ data: mocks.response }));
});

afterEach(cleanup);

describe("CliRetentionFold", () => {
  it("asks nothing until the fold is opened", () => {
    render(
      <MemoryRouter>
        <CliRetentionFold host={HOST} />
      </MemoryRouter>,
    );

    expect(mocks.enabled.every((enabled) => enabled === false)).toBe(true);
    expect(screen.queryByLabelText("Count a CLI as idle after, in minutes")).toBeNull();
  });

  it("holds no values for a Host still on the built-in rule, and says so", async () => {
    await renderFold();

    // Nothing is stored, so the built-in numbers are placeholders only: the
    // Host's idle main sessions are still closed by the built-in timeout.
    expect(idleInput()).toHaveValue(null);
    expect(idleInput()).toHaveAttribute("placeholder", "60");
    expect(limitInput()).toHaveValue(null);
    expect(limitInput()).toHaveAttribute("placeholder", "No limit");
    expect(screen.queryByTestId("cli-limit-warning")).toBeNull();
    expect(screen.getByTestId("cli-retention-status-host-a")).toHaveTextContent(
      "Using the built-in rule",
    );
    expect(screen.getByText(/No policy is saved for this Host/)).toBeInTheDocument();
    expect(screen.getByText(/takes that over for this Host/)).toBeInTheDocument();
    // No Save button anywhere: the fields themselves persist.
    expect(screen.queryByRole("button", { name: /^Save/ })).toBeNull();
  });

  it("persists a typed threshold at once, and nothing when it did not change", async () => {
    mocks.response = appliedResponse();
    await renderFold();

    fireEvent.change(idleInput(), { target: { value: "90" } });
    expect(mocks.replace).not.toHaveBeenCalled();
    fireEvent.blur(idleInput());

    expect(mocks.replace).toHaveBeenCalledWith(
      {
        expected_revision: 4,
        policy: {
          version: 1,
          idle_threshold_minutes: 90,
          max_idle_clis: 10,
          close_on_archive: true,
        },
      },
      expect.anything(),
    );

    mocks.replace.mockClear();
    fireEvent.change(idleInput(), { target: { value: "60" } });
    fireEvent.blur(idleInput());
    expect(mocks.replace).not.toHaveBeenCalled();
  });

  it("persists on Enter from a value field", async () => {
    mocks.response = appliedResponse();
    await renderFold();

    fireEvent.change(limitInput(), { target: { value: "6" } });
    fireEvent.keyDown(limitInput(), { key: "Enter" });

    expect(mocks.replace).toHaveBeenCalledWith(
      {
        expected_revision: 4,
        policy: {
          version: 1,
          idle_threshold_minutes: 60,
          max_idle_clis: 6,
          close_on_archive: true,
        },
      },
      expect.anything(),
    );
  });

  it("takes the policy over on the first edit of a Host on the built-in rule", async () => {
    await renderFold();

    fireEvent.change(idleInput(), { target: { value: "120" } });
    fireEvent.blur(idleInput());

    expect(mocks.replace).toHaveBeenCalledWith(
      {
        expected_revision: 0,
        policy: {
          version: 1,
          idle_threshold_minutes: 120,
          max_idle_clis: null,
          close_on_archive: true,
        },
      },
      expect.anything(),
    );
  });

  it("refuses an out-of-range threshold in place, without a request", async () => {
    await renderFold();

    fireEvent.change(idleInput(), { target: { value: "0" } });
    fireEvent.blur(idleInput());

    expect(screen.getByRole("alert")).toHaveTextContent("Enter whole minutes from 1 to 10,080.");
    expect(mocks.replace).not.toHaveBeenCalled();
    expect(idleInput()).toHaveValue(0);
  });

  it("clears the cap to no limit by emptying the field", async () => {
    mocks.response = appliedResponse();
    await renderFold();

    fireEvent.change(limitInput(), { target: { value: "" } });
    fireEvent.blur(limitInput());

    expect(mocks.replace).toHaveBeenCalledWith(
      {
        expected_revision: 4,
        policy: {
          version: 1,
          idle_threshold_minutes: 60,
          max_idle_clis: null,
          close_on_archive: true,
        },
      },
      expect.anything(),
    );
  });

  it("warns that no limit keeps idle CLIs resident, only once a policy holds one", async () => {
    mocks.response = unlimitedResponse();
    await renderFold();

    expect(screen.getByTestId("cli-limit-warning")).toHaveTextContent("No limit");
    expect(limitInput()).toHaveValue(null);
  });

  it("persists the close-on-archive switch at once", async () => {
    mocks.response = appliedResponse();
    await renderFold();

    fireEvent.click(screen.getByRole("switch", { name: "Close CLI on archive" }));

    expect(mocks.replace).toHaveBeenCalledWith(
      {
        expected_revision: 4,
        policy: {
          version: 1,
          idle_threshold_minutes: 60,
          max_idle_clis: 10,
          close_on_archive: false,
        },
      },
      expect.anything(),
    );
  });

  it("shows the applied state once and counts each family", async () => {
    mocks.response = appliedResponse();
    await renderFold();

    await screen.findByTestId("cli-family-table");
    expect(screen.getAllByText("Applied")).toHaveLength(1);
    const table = screen.getByTestId("cli-family-table");
    expect(within(table).getByText("Claude")).toBeInTheDocument();
    expect(within(table).getByText("Codex")).toBeInTheDocument();
    expect(within(table).getAllByText("Within limit")).toHaveLength(2);
    expect(within(table).getByText("8 / 10")).toBeInTheDocument();
  });

  it("asks before handing this Host back to the built-in rule", async () => {
    mocks.response = appliedResponse();
    await renderFold();

    fireEvent.click(await screen.findByRole("button", { name: "Use the built-in rule" }));
    const dialog = screen.getByRole("dialog", { name: "Use the built-in idle rule?" });
    expect(mocks.resetPolicy).not.toHaveBeenCalled();
    // The confirm answers and the trigger that opened it stay distinguishable.
    fireEvent.click(within(dialog).getByRole("button", { name: "Switch to the built-in rule" }));

    await waitFor(() => expect(mocks.resetPolicy).toHaveBeenCalledWith(4));
  });

  it("locks Close CLI on archive when Git already removes worktrees, and shows the way there", async () => {
    mocks.archiveMode = "green_only";
    mocks.response = appliedResponse();
    await renderFold();

    const toggle = await screen.findByRole("switch", { name: "Close CLI on archive" });
    expect(toggle).toBeDisabled();
    expect(toggle).toBeChecked();
    const locked = screen.getByTestId("cli-close-on-archive-locked");
    expect(locked).toHaveTextContent("Git already removes worktrees");
    expect(
      within(locked).getByRole("link", { name: "Change Worktrees on archive" }),
    ).toHaveAttribute("href", "/settings/git");
  });

  it("lists what was released in words, not field names", async () => {
    mocks.response = appliedResponse();
    mocks.releases = [
      {
        created_at: Math.floor(Date.now() / 1000) - 3600,
        reason: "idle_pool_overflow",
        family: "claude",
        status: "completed",
        target_session_id: "conv_0123456789abcdef",
      },
      {
        created_at: Math.floor(Date.now() / 1000) - 7200,
        reason: "archive",
        family: null,
        status: "pending",
        target_session_id: "conv_fedcba9876543210",
      },
    ];
    await renderFold();

    const log = await screen.findByTestId("cli-release-log");
    expect(log).toHaveTextContent("Over the idle limit");
    expect(log).toHaveTextContent("Session archived");
    expect(log).toHaveTextContent("Claude");
    expect(log).toHaveTextContent("waiting");
    expect(log).not.toHaveTextContent("idle_pool_overflow");
    expect(within(log).getAllByRole("link", { name: "Open session" })[0]).toHaveAttribute(
      "href",
      "/c/conv_0123456789abcdef",
    );
  });

  it("says so when nothing has been released on this Host", async () => {
    await renderFold();
    expect(
      await screen.findByText("No CLI has been released on this Host yet."),
    ).toBeInTheDocument();
  });
});
