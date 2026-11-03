// One Host's idle-CLI fold: what it shows before and after a policy exists,
// the blank-is-no-limit rule, the draft that survives a concurrent change, the
// built-in-rule reset, the Git lock on Close CLI on archive, and the release
// log in words instead of wire names.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Host, HostCliRelease, HostCliRetentionResponse } from "@/hooks/useHosts";

const legacyResponse = (): HostCliRetentionResponse => ({
  contract_version: 1,
  configured: false,
  revision: 0,
  policy: {
    version: 1,
    idle_threshold_minutes: 60,
    max_idle_clis: null,
    close_on_archive: true,
  },
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

const mocks = vi.hoisted(() => ({
  response: null as HostCliRetentionResponse | null,
  releases: [] as HostCliRelease[],
  reliability: { isError: false, error: null as unknown },
  replace: vi.fn(),
  resetPolicy: vi.fn(),
  refetch: vi.fn(),
  replaceError: null as unknown,
  replacePending: false,
  resetPending: false,
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
      mutateAsync: mocks.replace,
      isPending: mocks.replacePending,
      error: mocks.replaceError,
      reset: vi.fn(),
    }),
    useResetHostCliRetention: () => ({
      mutateAsync: mocks.resetPolicy,
      isPending: mocks.resetPending,
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

const HOST: Host = {
  host_id: "host-a",
  name: "Laptop",
  owner: "alice",
  status: "online",
};

function openFold() {
  const view = render(
    <MemoryRouter>
      <CliRetentionFold host={HOST} />
    </MemoryRouter>,
  );
  // jsdom reports a summary click but does not always flip the details element.
  const summary = view.container.querySelector("summary");
  if (summary === null) throw new Error("the fold has no summary");
  const details = summary.parentElement as HTMLDetailsElement;
  fireEvent.click(summary);
  if (!details.open) {
    details.open = true;
    fireEvent(details, new Event("toggle"));
  }
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
  mocks.replaceError = null;
  mocks.replacePending = false;
  mocks.resetPending = false;
  mocks.archiveMode = "never";
  mocks.enabled = [];
  mocks.replace.mockImplementation(async () => appliedResponse());
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

  it("shows the values the runtime uses before any policy is saved, blank meaning no limit", async () => {
    openFold();

    expect(await screen.findByLabelText("Count a CLI as idle after, in minutes")).toHaveValue(60);
    // The stored policy is null, so the field is blank rather than a made-up 10.
    expect(limitInput()).toHaveValue(null);
    expect(screen.getByTestId("cli-limit-warning")).toHaveTextContent("No limit");
    expect(screen.getByTestId("cli-retention-status-host-a")).toHaveTextContent(
      "Using the built-in rule",
    );

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(mocks.replace).toHaveBeenCalledWith({
        expected_revision: 0,
        policy: {
          version: 1,
          idle_threshold_minutes: 60,
          max_idle_clis: null,
          close_on_archive: true,
        },
      }),
    );
  });

  it("shows the applied state once and counts each family", async () => {
    mocks.response = appliedResponse();
    openFold();

    await screen.findByTestId("cli-family-table");
    expect(screen.getAllByText("Applied")).toHaveLength(1);
    const table = screen.getByTestId("cli-family-table");
    expect(within(table).getByText("Claude")).toBeInTheDocument();
    expect(within(table).getByText("Codex")).toBeInTheDocument();
    expect(within(table).getAllByText("Within limit")).toHaveLength(2);
    expect(within(table).getByText("8 / 10")).toBeInTheDocument();
    expect(screen.getByText(/^Read /)).toBeInTheDocument();
  });

  it("takes a number as the limit and drops the warning", async () => {
    mocks.response = appliedResponse();
    openFold();

    const limit = await screen.findByLabelText("Keep at most, per family");
    expect(screen.queryByTestId("cli-limit-warning")).toBeNull();
    fireEvent.change(limit, { target: { value: "6" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mocks.replace).toHaveBeenCalledWith({
        expected_revision: 4,
        policy: {
          version: 1,
          idle_threshold_minutes: 60,
          max_idle_clis: 6,
          close_on_archive: true,
        },
      }),
    );
  });

  it("refuses an out-of-range idle threshold without sending it", async () => {
    openFold();

    fireEvent.change(await screen.findByLabelText("Count a CLI as idle after, in minutes"), {
      target: { value: "0" },
    });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Use whole minutes from 1 to 10,080, and a limit from 0 to 100 or leave it blank.",
    );
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(mocks.replace).not.toHaveBeenCalled();
  });

  it("keeps the draft when the saved policy changes underneath, and reloads on request", async () => {
    mocks.response = appliedResponse();
    const view = render(
      <MemoryRouter>
        <CliRetentionFold host={HOST} />
      </MemoryRouter>,
    );
    const summary = view.container.querySelector("summary")!;
    const details = summary.parentElement as HTMLDetailsElement;
    fireEvent.click(summary);
    if (!details.open) {
      details.open = true;
      fireEvent(details, new Event("toggle"));
    }
    fireEvent.change(await screen.findByLabelText("Count a CLI as idle after, in minutes"), {
      target: { value: "90" },
    });

    mocks.response = {
      ...appliedResponse(),
      revision: 5,
      policy: { ...appliedResponse().policy, idle_threshold_minutes: 75 },
    };
    view.rerender(
      <MemoryRouter>
        <CliRetentionFold host={HOST} />
      </MemoryRouter>,
    );

    expect(idleInput()).toHaveValue(90);
    expect(screen.getByText(/saved policy changed while you were editing/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Reload current policy" }));
    await waitFor(() => expect(idleInput()).toHaveValue(75));
  });

  it("asks before handing this Host back to the built-in rule", async () => {
    mocks.response = appliedResponse();
    openFold();

    fireEvent.click(await screen.findByRole("button", { name: "Use the built-in rule" }));
    const dialog = screen.getByRole("dialog", { name: "Use the built-in idle rule?" });
    expect(mocks.resetPolicy).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Use the built-in rule" }));

    await waitFor(() => expect(mocks.resetPolicy).toHaveBeenCalledWith(4));
  });

  it("locks Close CLI on archive when Git already removes worktrees, and shows the way there", async () => {
    mocks.archiveMode = "green_only";
    mocks.response = appliedResponse();
    openFold();

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
    openFold();

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
    openFold();
    expect(
      await screen.findByText("No CLI has been released on this Host yet."),
    ).toBeInTheDocument();
  });
});
