import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { HostCliRetentionResponse } from "@/hooks/useHosts";

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

const mocks = vi.hoisted(() => ({
  response: null as HostCliRetentionResponse | null,
  replace: vi.fn(),
  resetPolicy: vi.fn(),
  refetch: vi.fn(),
  resetMutationState: vi.fn(),
  queryIsError: false,
  queryError: null as unknown,
  replaceError: null as unknown,
  resetError: null as unknown,
  replacePending: false,
  resetPending: false,
}));

vi.mock("@/hooks/useHosts", async () => {
  const actual = await vi.importActual("@/hooks/useHosts");
  return {
    ...actual,
    useHosts: () => ({
      data: [
        { host_id: "host-a", name: "PANDA-PC", owner: "alice", status: "online" },
        { host_id: "host-b", name: "FNOS", owner: "alice", status: "offline" },
      ],
      isLoading: false,
      isError: false,
    }),
    useHostCliRetention: () => ({
      data: mocks.response ?? undefined,
      isLoading: false,
      isError: mocks.queryIsError,
      error: mocks.queryError,
      refetch: mocks.refetch,
    }),
    useReplaceHostCliRetention: () => ({
      mutateAsync: mocks.replace,
      isPending: mocks.replacePending,
      error: mocks.replaceError,
      reset: mocks.resetMutationState,
    }),
    useResetHostCliRetention: () => ({
      mutateAsync: mocks.resetPolicy,
      isPending: mocks.resetPending,
      error: mocks.resetError,
      reset: mocks.resetMutationState,
    }),
  };
});

import { CliRetentionSettings } from "./CliRetentionSettings";

beforeEach(() => {
  mocks.response = legacyResponse();
  mocks.replace.mockReset();
  mocks.resetPolicy.mockReset();
  mocks.refetch.mockReset();
  mocks.resetMutationState.mockReset();
  mocks.queryIsError = false;
  mocks.queryError = null;
  mocks.replaceError = null;
  mocks.resetError = null;
  mocks.replacePending = false;
  mocks.resetPending = false;
  mocks.replace.mockImplementation(async () => mocks.response);
  mocks.resetPolicy.mockImplementation(async () => legacyResponse());
  mocks.refetch.mockImplementation(async () => ({ data: mocks.response }));
});

afterEach(cleanup);

describe("CliRetentionSettings", () => {
  it("prefills the approved bounded policy before taking over legacy ownership", async () => {
    render(<CliRetentionSettings />);

    expect(await screen.findByText("Using legacy rules")).toBeInTheDocument();
    expect(
      screen.getByText(/Existing pane and harness lifetime rules remain in control/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Runtime family counts will appear after you save/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Idle threshold in minutes")).toHaveValue(60);
    expect(screen.getByLabelText("Idle CLI limit per family")).toHaveValue(10);
    expect(screen.getByRole("checkbox", { name: "Unlimited" })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
    expect(screen.getByText(/Legacy rules active/)).toBeVisible();

    fireEvent.change(screen.getByLabelText("Idle threshold in minutes"), {
      target: { value: "61" },
    });
    expect(screen.getByText(/Unsaved changes/)).toBeVisible();
    fireEvent.change(screen.getByLabelText("Idle threshold in minutes"), {
      target: { value: "60" },
    });

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mocks.replace).toHaveBeenCalledWith({
        expected_revision: 0,
        policy: {
          version: 1,
          idle_threshold_minutes: 60,
          max_idle_clis: 10,
          close_on_archive: true,
        },
      }),
    );
  });

  it("renders structured family counts with their exact pool meaning", async () => {
    mocks.response = {
      contract_version: 1,
      configured: true,
      revision: 4,
      policy: {
        version: 1,
        idle_threshold_minutes: 60,
        max_idle_clis: 10,
        close_on_archive: true,
      },
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
    };

    render(<CliRetentionSettings />);

    expect((await screen.findAllByText("Applied")).length).toBeGreaterThan(0);
    expect(screen.getByText("Claude")).toBeInTheDocument();
    expect(screen.getByText("8 / 10", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("Active 4 · Below threshold 2 · Total 14")).toBeInTheDocument();
    expect(screen.getByText("Codex")).toBeInTheDocument();
    expect(screen.getByText("Active 2 · Below threshold 1 · Total 6")).toBeInTheDocument();
    expect(screen.getByText(/Retained idle CLIs keep Runners/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Restore legacy rules/ })).toBeEnabled();
  });

  it("shows a partial legacy restoration when reset runtimes remain unavailable", async () => {
    mocks.response = {
      ...legacyResponse(),
      revision: 2,
      runtime: {
        configured: false,
        policy_revision: 2,
        observed_at: 1_900_000_000,
        families: {},
        reset: ["session-reset"],
        unavailable: ["session-unavailable"],
      },
      application: { status: "partial", policy_revision: 2, observed_at: 1_900_000_000 },
    };

    render(<CliRetentionSettings />);

    expect(await screen.findByText("Partially restored legacy rules")).toBeVisible();
    expect(screen.getByText(/unavailable runtimes still need to be reset/i)).toBeVisible();
    expect(screen.queryByText(/Legacy rules active/)).not.toBeInTheDocument();
    expect(screen.getByText(/Runtime reset is not fully confirmed/)).toBeVisible();
  });

  it("preserves a draft when polling observes another revision and reloads on request", async () => {
    mocks.response = {
      ...legacyResponse(),
      configured: true,
      revision: 2,
      policy: { ...legacyResponse().policy, max_idle_clis: 10 },
      application: { status: "applied", policy_revision: 2, observed_at: 1_900_000_000 },
    };
    const { rerender } = render(<CliRetentionSettings />);
    const input = await screen.findByLabelText("Idle threshold in minutes");
    fireEvent.change(input, { target: { value: "90" } });

    mocks.response = {
      ...mocks.response,
      revision: 3,
      policy: { ...mocks.response.policy, idle_threshold_minutes: 75 },
      application: { ...mocks.response.application, policy_revision: 3 },
    };
    rerender(<CliRetentionSettings />);

    expect(screen.getByLabelText("Idle threshold in minutes")).toHaveValue(90);
    expect(screen.getByText(/saved policy changed while you were editing/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Reload current policy" }));
    await waitFor(() => expect(screen.getByLabelText("Idle threshold in minutes")).toHaveValue(75));
  });

  it("restores the existing layered lifetime rules with the current revision", async () => {
    mocks.response = {
      ...legacyResponse(),
      configured: true,
      revision: 6,
      policy: { ...legacyResponse().policy, max_idle_clis: 5 },
      application: { status: "pending", policy_revision: 6, observed_at: null },
    };
    render(<CliRetentionSettings />);

    fireEvent.click(await screen.findByRole("button", { name: /Restore legacy rules/ }));

    const dialog = screen.getByRole("dialog", { name: "Restore legacy CLI rules?" });
    expect(
      within(dialog).getByText(
        /returns retained panes and harnesses to the existing TTL cleanup rules/,
      ),
    ).toBeInTheDocument();
    expect(mocks.resetPolicy).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore legacy rules" }));

    await waitFor(() => expect(mocks.resetPolicy).toHaveBeenCalledWith(6));
  });

  it("keeps a failed legacy reset error visible inside the open confirmation", async () => {
    mocks.response = {
      ...legacyResponse(),
      configured: true,
      revision: 6,
      policy: { ...legacyResponse().policy, max_idle_clis: 5 },
      application: { status: "pending", policy_revision: 6, observed_at: null },
    };
    const { rerender } = render(<CliRetentionSettings />);
    fireEvent.click(await screen.findByRole("button", { name: /Restore legacy rules/ }));

    const failure = new Error("Reset request failed");
    mocks.resetError = failure;
    mocks.resetPolicy.mockRejectedValueOnce(failure);
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Restore legacy rules" }),
    );
    await waitFor(() => expect(mocks.resetPolicy).toHaveBeenCalledWith(6));
    rerender(<CliRetentionSettings />);

    const dialog = screen.getByRole("dialog", { name: "Restore legacy CLI rules?" });
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Reset request failed");
    expect(dialog).toBeVisible();
  });

  it("warns when polling fails while showing cached policy and runtime data", async () => {
    mocks.response = {
      ...legacyResponse(),
      configured: true,
      revision: 7,
      policy: { ...legacyResponse().policy, max_idle_clis: 10 },
      application: { status: "applied", policy_revision: 7, observed_at: 1_900_000_000 },
    };
    mocks.queryIsError = true;
    mocks.queryError = new Error("Network unavailable");

    render(<CliRetentionSettings />);

    expect(
      await screen.findByText(/Showing the last successful policy and runtime/),
    ).toHaveTextContent("Network unavailable");
    expect(screen.getByRole("button", { name: "Retry refresh" })).toBeEnabled();
    expect(screen.getAllByText("Applied").length).toBeGreaterThan(0);
  });

  it("locks the Host and policy controls while a save is pending", async () => {
    mocks.replacePending = true;
    const { rerender } = render(<CliRetentionSettings />);

    expect(await screen.findByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Host" })).toBeDisabled();
    expect(screen.getByLabelText("Idle threshold in minutes")).toBeDisabled();
    expect(screen.getByLabelText("Idle CLI limit per family")).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "Unlimited" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Close CLI on archive" })).toBeDisabled();

    mocks.replacePending = false;
    rerender(<CliRetentionSettings />);

    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
    expect(screen.getByRole("combobox", { name: "Host" })).toBeEnabled();
    expect(screen.getByLabelText("Idle threshold in minutes")).toBeEnabled();
    expect(screen.getByLabelText("Idle CLI limit per family")).toBeEnabled();
    expect(screen.getByRole("checkbox", { name: "Unlimited" })).toBeEnabled();
    expect(screen.getByRole("switch", { name: "Close CLI on archive" })).toBeEnabled();
  });

  it("blocks a legacy reset when the policy changes while confirmation is open", async () => {
    mocks.response = {
      ...legacyResponse(),
      configured: true,
      revision: 6,
      policy: { ...legacyResponse().policy, max_idle_clis: 5 },
      application: { status: "pending", policy_revision: 6, observed_at: null },
    };
    const { rerender } = render(<CliRetentionSettings />);
    fireEvent.change(await screen.findByLabelText("Idle threshold in minutes"), {
      target: { value: "90" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Restore legacy rules/ }));

    mocks.response = {
      ...mocks.response,
      revision: 7,
      policy: { ...mocks.response.policy, idle_threshold_minutes: 75 },
      application: { ...mocks.response.application, policy_revision: 7 },
    };
    rerender(<CliRetentionSettings />);

    const dialog = screen.getByRole("dialog", { name: "Restore legacy CLI rules?" });
    expect(
      within(dialog).getByText(/saved policy changed after this confirmation opened/i),
    ).toBeVisible();
    expect(within(dialog).getByRole("button", { name: "Restore legacy rules" })).toBeDisabled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Reload current policy" }));
    await waitFor(() =>
      expect(within(dialog).getByRole("button", { name: "Restore legacy rules" })).toBeEnabled(),
    );
    expect(screen.getByLabelText("Idle threshold in minutes")).toHaveValue(75);
  });
});
