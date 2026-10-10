// Tests for the admin GlobalInstructionsPage (server-held global instructions:
// live text, large saves, revision restore).
//
// Browser e2e is impractical (admin/accounts-gated), so the surface is pinned
// here by mocking the mode-agnostic identity probe (resolveIdentity /
// getCurrentIsAdmin) and the react-query hooks so no QueryClient or network is
// needed — same approach as PoliciesPage.test.tsx.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GlobalInstructionsPage } from "./GlobalInstructionsPage";
import * as identity from "@/lib/identity";
import * as globalInstructions from "@/hooks/useGlobalInstructions";

const serverInfoMocks = vi.hoisted(() => ({
  accountsEnabled: true,
  loginUrl: null as string | null,
  serverVersion: "0.3.0.dev0" as string | null,
  singleUser: false,
}));

vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    accounts_enabled: serverInfoMocks.accountsEnabled,
    login_url: serverInfoMocks.loginUrl,
    server_version: serverInfoMocks.serverVersion,
    single_user: serverInfoMocks.singleUser,
  }),
}));

vi.mock("@/lib/identity", () => ({
  resolveIdentity: vi.fn(),
  getCurrentIsAdmin: vi.fn(),
}));
vi.mock("@/hooks/useGlobalInstructions", () => ({
  useGlobalInstructions: vi.fn(),
  useGlobalInstructionRevisions: vi.fn(),
  useSaveGlobalInstructions: vi.fn(),
}));

type Current = ReturnType<typeof current>;
function current(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    text: "",
    revision_id: null,
    updated_at: null,
    updated_by: null,
    max_chars: null,
    ...overrides,
  };
}

type Revision = ReturnType<typeof revision>;
function revision(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: "r1",
    text: "older text",
    created_at: 1_752_000_000,
    created_by: "admin",
    ...overrides,
  };
}

const refetchMock = vi.fn();
const saveMutate = vi.fn();

function setCurrent(value: Current) {
  vi.mocked(globalInstructions.useGlobalInstructions).mockReturnValue({
    data: value,
    isError: false,
    error: null,
    refetch: refetchMock,
  } as never);
}

function setRevisions(list: Revision[]) {
  vi.mocked(globalInstructions.useGlobalInstructionRevisions).mockReturnValue({
    data: list,
  } as never);
}

function renderPage() {
  return render(
    <MemoryRouter>
      <GlobalInstructionsPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  // Explicit single-user local runtime: no auth endpoints, so the admin probe
  // is skipped and the page renders directly (matches PoliciesPage.test.tsx).
  serverInfoMocks.accountsEnabled = false;
  serverInfoMocks.loginUrl = null;
  serverInfoMocks.serverVersion = "0.3.0.dev0";
  serverInfoMocks.singleUser = true;
  vi.mocked(identity.resolveIdentity).mockResolvedValue("admin");
  vi.mocked(identity.getCurrentIsAdmin).mockReturnValue(true);
  setCurrent(current());
  setRevisions([]);
  saveMutate.mockReset();
  vi.mocked(globalInstructions.useSaveGlobalInstructions).mockReturnValue({
    mutate: saveMutate,
    isPending: false,
    isError: false,
    error: null,
  } as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("GlobalInstructionsPage", () => {
  it("saves a large draft with a standalone length counter", async () => {
    renderPage();

    const editor = await screen.findByLabelText("Global instructions");
    const save = screen.getByRole("button", { name: /Save/ });

    // Unchanged draft → nothing to save.
    expect(save).toBeDisabled();

    const text = "x".repeat(65536);
    fireEvent.change(editor, { target: { value: text } });
    expect(screen.getByText("65536 characters")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(save).toBeEnabled();
    fireEvent.click(save);
    expect(saveMutate).toHaveBeenCalledWith(text);
  });

  it("keeps edits typed while a save is pending when the query resolves with the old text", async () => {
    setCurrent(current({ text: "A" }));
    const { rerender } = renderPage();

    const editor = await screen.findByLabelText("Global instructions");
    expect(editor).toHaveValue("A");

    // Save "A" is in flight while the admin keeps typing.
    fireEvent.change(editor, { target: { value: "AB" } });
    fireEvent.click(screen.getByRole("button", { name: /Save/ }));
    expect(saveMutate).toHaveBeenCalledWith("AB");

    // The post-save refetch still returns the pre-edit text.
    setCurrent(current({ text: "A" }));
    rerender(
      <MemoryRouter>
        <GlobalInstructionsPage />
      </MemoryRouter>,
    );

    expect(editor).toHaveValue("AB");
    expect(screen.getByRole("button", { name: /Save/ })).toBeEnabled();
  });

  it("counts Unicode code points", async () => {
    renderPage();

    const editor = await screen.findByLabelText("Global instructions");
    // Two emoji: four UTF-16 units but two code points.
    fireEvent.change(editor, { target: { value: "😀😀" } });

    expect(screen.getByText("2 characters")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Save/ })).toBeEnabled();
  });

  it("loads a revision's text into the editor when clicked", async () => {
    setCurrent(current({ text: "current text" }));
    setRevisions([revision({ text: "older text" })]);
    renderPage();

    const editor = await screen.findByLabelText("Global instructions");
    expect(editor).toHaveValue("current text");

    fireEvent.click(screen.getByRole("button", { name: /older text/ }));

    expect(editor).toHaveValue("older text");
  });
});
