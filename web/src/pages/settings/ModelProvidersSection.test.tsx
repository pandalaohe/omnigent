import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "@/lib/modelConnectionsApi";
import { ModelProvidersSection } from "./ModelProvidersSection";

vi.mock("@/lib/modelConnectionsApi", async (original) => ({
  ...(await original<typeof api>()),
  listModelConnections: vi.fn(),
  getModelCatalog: vi.fn(),
  refreshModelCatalog: vi.fn(),
  saveModelConnection: vi.fn(),
  deleteModelConnection: vi.fn(),
  fetchConnectionModels: vi.fn(),
}));
const row: api.ModelConnection = {
  name: "new",
  display_name: "Example provider",
  kind: "key",
  base_url: "https://provider.example/v1",
  wire_api: "chat",
  key_set: true,
  key_source: "stored",
  editable: true,
  models: [
    {
      key: "speech",
      display_name: "Speech",
      model_id: "speech-v1",
      status: "listed",
      used_by: { users: 2, slots: [{ feature: "Voice input", slot: "Speech to text", users: 2 }] },
    },
    { key: "free", display_name: "Unused", model_id: "chat-v1", status: "gone" },
  ],
  catalog: { fetched_at: "2026-11-01T00:00:00Z", ids: ["speech-v1", "chat-v2"], complete: true },
  used_by: 2,
};
afterEach(cleanup);
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.listModelConnections).mockResolvedValue({ connections: [row], can_manage: true });
  vi.mocked(api.getModelCatalog).mockResolvedValue(row.catalog!);
  vi.mocked(api.refreshModelCatalog).mockResolvedValue(row);
  vi.mocked(api.saveModelConnection).mockResolvedValue(row);
});
async function page(path = "/settings/model-providers/new") {
  render(
    <MemoryRouter initialEntries={[path]}>
      <ModelProvidersSection />
    </MemoryRouter>,
  );
  await screen.findByRole("heading", {
    name: path.includes("?create")
      ? "New model provider"
      : path.endsWith("/new")
        ? "Example provider"
        : "Model providers",
  });
}

it("opens a provider named new, renders fixed columns and compact accessible status/usage", async () => {
  await page();
  expect(screen.getByRole("table")).toHaveClass("table-fixed");
  expect(screen.getByLabelText("Provider id")).toHaveAttribute("readonly");
  expect(screen.getByRole("img", { name: "Listed by the provider" })).toBeVisible();
  expect(screen.getByRole("img", { name: "No longer listed by the provider" })).toBeVisible();
  expect(screen.getByRole("button", { name: /In use: Speech to text, 2 users/ })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Remove provider" })).toBeDisabled();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Used by 2 users" }));
  expect(screen.getByText("Voice input · Speech to text: 2 users")).toBeVisible();
  await user.keyboard("{Escape}");
  expect(screen.queryByText("Voice input · Speech to text: 2 users")).toBeNull();
});

it("clears filters, focuses the added model and removes an empty new row on blur", async () => {
  await page();
  fireEvent.change(screen.getByLabelText("Filter models"), { target: { value: "missing" } });
  fireEvent.click(screen.getByRole("radio", { name: "Needs attention" }));
  fireEvent.click(screen.getByRole("button", { name: "Add model" }));
  expect(screen.getByLabelText("Filter models")).toHaveValue("");
  const inputs = screen
    .getAllByRole("combobox")
    .filter((input) => input.getAttribute("aria-label")?.startsWith("Requested model"));
  const added = inputs.at(-1)!;
  expect(added).toHaveFocus();
  expect(added).toHaveAttribute("aria-expanded", "true");
  fireEvent.blur(added, { relatedTarget: screen.getByRole("button", { name: "Save" }) });
  expect(screen.getAllByRole("row")).toHaveLength(3);
});

it("counts unsaved rows toward the 50-row cap", async () => {
  vi.mocked(api.listModelConnections).mockResolvedValue({
    can_manage: true,
    connections: [
      {
        ...row,
        models: Array.from({ length: 49 }, (_, i) => ({
          key: `row-${i}`,
          display_name: `Model ${i}`,
          model_id: `id-${i}`,
          status: "unchecked",
        })),
      },
    ],
  });
  await page();
  fireEvent.click(screen.getByRole("button", { name: "Add model" }));
  expect(screen.getByRole("button", { name: "Limit of 50 models reached" })).toBeDisabled();
}, 15000);

it("preserves draft rows across refresh, then discards them", async () => {
  vi.mocked(api.refreshModelCatalog).mockResolvedValue({
    ...row,
    used_by: 0,
    models: row.models.map((model) => ({ ...model, used_by: undefined })),
  });
  await page();
  fireEvent.change(screen.getByLabelText("Display name free"), { target: { value: "Draft name" } });
  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "Refresh model catalogue" })),
  );
  expect(screen.getByLabelText("Display name free")).toHaveValue("Draft name");
  expect(screen.getByRole("button", { name: "Remove provider" })).toBeEnabled();
  expect(screen.getAllByRole("button", { name: "Remove model" })).toHaveLength(2);
  expect(screen.queryByRole("button", { name: "Used by 2 users" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Discard" }));
  expect(screen.getByLabelText("Display name free")).toHaveValue("Unused");
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  expect(screen.getAllByRole("button", { name: "Remove model" })).toHaveLength(2);
});

it("refreshes new usage by stable key while preserving requested-id edits and row removals", async () => {
  const unused = {
    ...row,
    used_by: 0,
    models: row.models.map((model) => ({ ...model, used_by: undefined })),
  };
  vi.mocked(api.listModelConnections).mockResolvedValue({
    connections: [unused],
    can_manage: true,
  });
  await page();
  const user = userEvent.setup();
  const requested = screen.getByRole("combobox", { name: "Requested model speech" });
  await user.clear(requested);
  await user.type(requested, "draft-id{Enter}");
  fireEvent.click(
    within(screen.getByLabelText("Display name free").closest("tr")!).getByRole("button", {
      name: "Remove model",
    }),
  );
  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "Refresh model catalogue" })),
  );
  expect(requested).toHaveValue("draft-id");
  expect(screen.queryByLabelText("Display name free")).toBeNull();
  expect(screen.getByRole("button", { name: "Used by 2 users" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Remove provider" })).toBeDisabled();
  fireEvent.click(screen.getByRole("radio", { name: "In use" }));
  expect(screen.getByLabelText("Display name speech")).toBeVisible();
});

it("restores the saved key source when discarding an edit", async () => {
  await page();
  await userEvent.click(screen.getByRole("combobox", { name: "Key source" }));
  await userEvent.click(screen.getByRole("option", { name: "Environment variable" }));
  fireEvent.change(screen.getByLabelText("Environment variable name"), {
    target: { value: "EXAMPLE_PROVIDER_KEY" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Discard" }));
  expect(screen.getByLabelText("API key")).toBeVisible();
});

it("saves stable keys and restores a concurrently referenced removed row with a lock reason", async () => {
  vi.mocked(api.saveModelConnection).mockRejectedValue(
    new api.ModelConnectionError(409, {
      kind: "in_use",
      rows: { free: { users: 1, slots: [{ feature: "Voice input", slot: "Cleanup", users: 1 }] } },
    }),
  );
  await page();
  fireEvent.click(screen.getByRole("button", { name: "Remove model" }));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));
  expect(api.saveModelConnection).toHaveBeenCalledWith(
    "new",
    expect.objectContaining({ models: { speech: "speech-v1" } }),
    false,
  );
  expect(screen.getByLabelText("Display name free")).toHaveValue("Unused");
  expect(screen.getByRole("button", { name: /In use: Cleanup, 1 users/ })).toBeDisabled();
  await act(async () =>
    fireEvent.click(screen.getByRole("button", { name: "Refresh model catalogue" })),
  );
  expect(screen.queryByRole("button", { name: /In use: Cleanup/ })).toBeNull();
  expect(
    within(screen.getByLabelText("Display name free").closest("tr")!).getByRole("button", {
      name: "Remove model",
    }),
  ).toBeEnabled();
});

it("requires confirmation before deleting an unused provider and stored key", async () => {
  vi.mocked(api.listModelConnections).mockResolvedValue({
    can_manage: true,
    connections: [{ ...row, used_by: 0 }],
  });
  await page();
  fireEvent.click(screen.getByRole("button", { name: "Remove provider" }));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByText(/stored key will be deleted/)).toBeVisible();
  expect(api.deleteModelConnection).not.toHaveBeenCalled();
  await act(async () =>
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove provider" })),
  );
  expect(api.deleteModelConnection).toHaveBeenCalledWith("new");
});

it("generates an editable creation id, suggests another id on collision, and uses create mode", async () => {
  vi.mocked(api.saveModelConnection).mockRejectedValue(
    new api.ModelConnectionError(409, { kind: "exists" }),
  );
  await page("/settings/model-providers?create");
  fireEvent.change(screen.getByLabelText("Provider display name"), {
    target: { value: "Example gateway" },
  });
  expect(screen.getByLabelText("Provider id")).toHaveValue("example-gateway");
  fireEvent.change(screen.getByLabelText("Provider id"), { target: { value: "custom-id" } });
  fireEvent.change(screen.getByLabelText("Provider display name"), {
    target: { value: "Renamed" },
  });
  expect(screen.getByLabelText("Provider id")).toHaveValue("custom-id");
  fireEvent.change(screen.getByLabelText("Endpoint"), {
    target: { value: "https://provider.example/v1" },
  });
  fireEvent.change(screen.getByLabelText("API key"), { target: { value: "fake-key" } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save" })));
  expect(api.saveModelConnection).toHaveBeenCalledWith(
    "custom-id",
    expect.objectContaining({ display_name: "Renamed" }),
    true,
  );
  expect(screen.getByLabelText("Provider id")).toHaveValue("custom-id-2");
});
