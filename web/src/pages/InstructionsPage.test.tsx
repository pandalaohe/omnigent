import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { InstructionsPage } from "./InstructionsPage";
import type {
  InstructionLayer,
  InstructionLayers,
  InstructionPreview,
} from "@/hooks/useInstructionLayers";
import type * as InstructionHooks from "@/hooks/useInstructionLayers";
import type * as NativeBridge from "@/lib/nativeBridge";

const mocks = vi.hoisted(() => ({
  admin: true,
  ios: false,
  layers: null as InstructionLayers | null,
  preview: null as InstructionPreview | null,
  save: vi.fn(),
  copy: vi.fn(),
  previewQuery: vi.fn(),
}));
vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => mocks.admin }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ single_user: false, accounts_enabled: true }),
}));
vi.mock("@/hooks/useHosts", () => ({
  useHosts: (options: { includeSandbox?: boolean } = {}) => ({
    data: [
      { host_id: "host-a", name: "Laptop", configured_harnesses: { "codex-native": true } },
      ...(options.includeSandbox
        ? [{ host_id: "host-cloud", name: "Cloud", sandbox_provider: "managed" }]
        : []),
    ],
    isError: false,
  }),
}));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridge>()),
  isIOSShell: () => mocks.ios,
}));
vi.mock("@/components/ai-elements/message", () => ({
  MessageResponse: ({ children }: { children: string }) => <div>{children}</div>,
}));
vi.mock("@/lib/clipboard", () => ({ copyText: (text: string) => mocks.copy(text) }));
vi.mock("@/hooks/useInstructionLayers", async (importOriginal) => ({
  ...(await importOriginal<typeof InstructionHooks>()),
  useInstructionLayers: () => ({ data: mocks.layers, isPending: false, isError: false }),
  useInstructionPreview: (query: URLSearchParams | null) => {
    mocks.previewQuery(query?.toString());
    return { data: query ? mocks.preview : undefined, isPending: false, isError: false };
  },
  useSaveInstructionLayer: () => ({ mutateAsync: mocks.save, isPending: false, isError: false }),
  useInstructionRevisions: () => ({
    data: { data: [{ id: "old", text: "Older shared", created_at: 100, created_by: "Admin" }] },
  }),
}));

function layer(
  scope: InstructionLayer["scope"],
  text: string,
  agent_key = "",
  host_id = "",
): InstructionLayer {
  return {
    scope,
    text,
    agent_key,
    host_id,
    revision_id: scope,
    updated_at: 1,
    updated_by: null,
    chars: [...text].length,
    host_removed: false,
    stale_layers: [],
  };
}
function Location() {
  return <output data-testid="location">{useLocation().search}</output>;
}
function page(url = "/settings/instructions") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <InstructionsPage />
      <Location />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mocks.admin = true;
  mocks.ios = false;
  mocks.layers = {
    data: [
      layer("shared", "Shared text"),
      layer("agent", "Agent text", "codex"),
      layer("host", "Host text", "", "host-a"),
    ],
    agents: [{ key: "codex", display_name: "Codex" }],
    order: ["shared", "agent", "host"],
    route_sizes: [],
    size_reminder_chars: 10000,
  };
  mocks.preview = {
    subject: { name: "Codex terminal agent", harness: "codex-native" },
    route: { host_id: "host-a", agent_key: "codex", child: false, peer: true, spawn: true },
    parts: [
      {
        kind: "user_layers",
        scope: "shared",
        agent_key: "",
        host_id: "",
        text: "Shared text",
        chars: 11,
        sent: true,
        readonly: false,
      },
      { kind: "framework", text: "Framework text", chars: 14, sent: true, readonly: true },
    ],
    text: "Shared text\n\nFramework text",
    total_chars: 27,
    framework_unavailable: false,
  };
  mocks.save.mockReset().mockResolvedValue({ over_budget_routes: [] });
  mocks.copy.mockReset().mockResolvedValue(undefined);
  mocks.previewQuery.mockClear();
});
afterEach(cleanup);

describe("InstructionsPage", () => {
  it("includes managed hosts in the matrix and route picker", async () => {
    page();
    expect(screen.getByRole("button", { name: "Edit Cloud / Codex" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Route total" }));
    fireEvent.click(screen.getByRole("combobox", { name: "Host" }));
    expect(await screen.findByRole("option", { name: "Cloud" })).toBeInTheDocument();
  });
  it("preserves a session's missing host and product without editing a different route", () => {
    mocks.preview!.route.host_id = null;
    mocks.preview!.route.agent_key = null;
    page("/settings/instructions?session=session-a");
    expect(screen.getByRole("combobox", { name: "Host" })).toHaveTextContent("Select host");
    expect(screen.getByRole("combobox", { name: "Agent" })).toHaveTextContent("Select agent");
    expect(
      screen.queryByRole("button", { name: "Edit independent setting" }),
    ).not.toBeInTheDocument();
  });
  it("reapplies bulk disclosure actions after individual sections change", async () => {
    page("/settings/instructions?view=route");
    const details = screen.getByText("Framework text").closest("details")!;
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
    expect(details.open).toBe(true);
    details.open = false;
    fireEvent(details, new Event("toggle"));
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
    await waitFor(() => expect(details.open).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Collapse all" }));
    details.open = true;
    fireEvent(details, new Event("toggle"));
    fireEvent.click(screen.getByRole("button", { name: "Collapse all" }));
    await waitFor(() => expect(details.open).toBe(false));
  });
  it("pins the iOS editor within the visible viewport and safe areas", () => {
    mocks.ios = true;
    page();
    fireEvent.click(screen.getByRole("button", { name: "Edit Shared" }));
    expect(screen.getByRole("dialog").style.top).toBe("var(--omnigent-safe-top, 0px)");
    expect(screen.getByRole("dialog").style.height).toContain("--omnigent-viewport-height");
    expect(screen.getByRole("dialog").style.maxHeight).toContain("--omnigent-safe-bottom");
  });
  it("searches a route picker and retains an addressable host and agent", async () => {
    page("/settings/instructions?view=route");
    fireEvent.click(screen.getByRole("combobox", { name: "Host" }));
    fireEvent.change(screen.getByPlaceholderText("Search host…"), { target: { value: "Lap" } });
    fireEvent.click(await screen.findByRole("option", { name: "Laptop" }));
    expect(screen.getByTestId("location")).toHaveTextContent("host=host-a&agent=codex");
  });
  it("warns about new over-budget routes while retaining the saved text", async () => {
    mocks.save.mockResolvedValue({
      over_budget_routes: [{ host_id: "host-a", agent_key: "codex", total_chars: 10001 }],
    });
    page();
    fireEvent.click(screen.getByRole("button", { name: "Edit Shared" }));
    fireEvent.change(screen.getByLabelText("Instruction text"), { target: { value: "Changed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save layer" }));
    await screen.findByText(/Newly over 10,000 characters/);
    expect(screen.getByLabelText("Instruction text")).toHaveValue("Changed");
  });
  it("clears an independent setting with a new blank revision", async () => {
    mocks.layers!.data.push(layer("independent", "Independent text", "codex", "host-a"));
    page();
    fireEvent.click(screen.getByRole("button", { name: "Edit Laptop / Codex" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove independent setting" }));
    await waitFor(() =>
      expect(mocks.save).toHaveBeenCalledWith({
        scope: "independent",
        agent_key: "codex",
        host_id: "host-a",
        text: "",
      }),
    );
  });
  it("edits a scoped layer, counts Unicode, restores history and saves large text intact", async () => {
    page();
    fireEvent.click(screen.getByRole("button", { name: "Edit Shared" }));
    const editor = screen.getByLabelText("Instruction text");
    expect(editor).toHaveValue("Shared text");
    expect(screen.getByRole("button", { name: "Save layer" })).toBeDisabled();
    fireEvent.change(editor, { target: { value: "😀😀" } });
    expect(screen.getByText("2 characters · Unsaved changes")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Older shared/ }));
    expect(editor).toHaveValue("Older shared");
    const text = "x".repeat(65536);
    fireEvent.change(editor, { target: { value: text } });
    fireEvent.click(screen.getByRole("button", { name: "Save layer" }));
    await waitFor(() =>
      expect(mocks.save).toHaveBeenCalledWith({
        scope: "shared",
        agent_key: "",
        host_id: "",
        text,
      }),
    );
  });
  it("keeps a newer draft when an earlier save completes", async () => {
    let finish: (value: { over_budget_routes: [] }) => void = () => {};
    mocks.save.mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    page();
    fireEvent.click(screen.getByRole("button", { name: "Edit Shared" }));
    const editor = screen.getByLabelText("Instruction text");
    fireEvent.change(editor, { target: { value: "AB" } });
    fireEvent.click(screen.getByRole("button", { name: "Save layer" }));
    fireEvent.change(editor, { target: { value: "ABC" } });
    await act(async () => finish({ over_budget_routes: [] }));
    expect(editor).toHaveValue("ABC");
    expect(screen.getByRole("button", { name: "Save layer" })).toBeEnabled();
  });
  it("prefills an independent setting from shared → agent → host", () => {
    page();
    fireEvent.click(screen.getByRole("button", { name: "Edit Laptop / Codex" }));
    expect(screen.getByLabelText("Instruction text")).toHaveValue(
      "Shared text\n\nAgent text\n\nHost text",
    );
    expect(mocks.save).not.toHaveBeenCalled();
  });
  it("saves an untouched prefill as an independent setting without marking it dirty", async () => {
    page("/settings/instructions?view=route");
    fireEvent.click(screen.getByRole("button", { name: "Edit independent setting" }));
    expect(screen.queryByText(/· Unsaved changes/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save layer" }));
    await screen.findByText("Layer saved.");
    expect(mocks.save).toHaveBeenCalledWith({
      scope: "independent",
      agent_key: "codex",
      host_id: "host-a",
      text: "Shared text\n\nAgent text\n\nHost text",
    });
    expect(screen.getByRole("button", { name: "Save layer" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it.each(["Close", "Escape"])("closes an untouched independent draft with %s", async (action) => {
    page();
    fireEvent.click(screen.getByRole("button", { name: "Edit Laptop / Codex" }));
    expect(screen.getByLabelText("Instruction text")).toHaveValue(
      "Shared text\n\nAgent text\n\nHost text",
    );
    if (action === "Close") fireEvent.click(screen.getByRole("button", { name: "Close" }));
    else await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mocks.save).not.toHaveBeenCalled();
  });
  it("keeps the discard prompt above long edited text and discards only on request", async () => {
    page();
    fireEvent.click(screen.getByRole("button", { name: "Edit Laptop / Codex" }));
    const editor = screen.getByLabelText("Instruction text");
    fireEvent.change(editor, { target: { value: "Edited instructions\n".repeat(500) } });
    await userEvent.keyboard("{Escape}");
    const prompt = screen.getByRole("alert");
    expect(prompt).toHaveTextContent("You have unsaved changes.");
    expect(prompt.compareDocumentPosition(editor) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(prompt).toHaveClass("sticky", "top-0");
    fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(editor).toHaveValue("Edited instructions\n".repeat(500));
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mocks.save).not.toHaveBeenCalled();
  });
  it.each(["Shared", "Codex", "Laptop"])("preserves dirty tracking for the %s layer", (name) => {
    page();
    fireEvent.click(screen.getByRole("button", { name: `Edit ${name}` }));
    const editor = screen.getByLabelText("Instruction text");
    const original = (editor as HTMLTextAreaElement).value;
    expect(screen.getByRole("button", { name: "Save layer" })).toBeDisabled();
    fireEvent.change(editor, { target: { value: "Changed" } });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.getByRole("alert")).toHaveTextContent("You have unsaved changes.");
    fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
    fireEvent.change(editor, { target: { value: original } });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it("reads the selected URL route and copies the exact composed total", async () => {
    page("/settings/instructions?view=route&host=host-a&agent=codex&child=1&peer=0");
    expect(mocks.previewQuery).toHaveBeenLastCalledWith(
      "host_id=host-a&agent_key=codex&child=true&peer=false",
    );
    fireEvent.click(screen.getByRole("button", { name: "Copy all" }));
    await waitFor(() => expect(mocks.copy).toHaveBeenCalledWith("Shared text\n\nFramework text"));
  });
  it("previews a session's own route and allows readers without edit controls", () => {
    mocks.admin = false;
    page("/settings/instructions?session=session-a");
    expect(mocks.previewQuery).toHaveBeenLastCalledWith("session_id=session-a");
    expect(screen.getByText("Codex terminal agent")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Edit independent setting" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit layer" })).not.toBeInTheDocument();
  });
  it("marks replaced layers and incomplete framework previews", () => {
    mocks.preview!.parts.push({
      kind: "user_layers",
      scope: "host",
      host_id: "host-a",
      agent_key: "",
      text: "Replaced",
      chars: 8,
      sent: false,
      readonly: false,
    });
    mocks.preview!.framework_unavailable = true;
    page("/settings/instructions?view=route");
    expect(screen.getByText(/Not sent on this route/)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("This preview is incomplete");
  });
});
