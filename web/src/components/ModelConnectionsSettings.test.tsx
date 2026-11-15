import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "@/lib/modelConnectionsApi";
import { ModelConnectionsSettings } from "./ModelConnectionsSettings";

vi.mock("@/lib/modelConnectionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  listModelConnections: vi.fn(),
  fetchConnectionModels: vi.fn(),
  saveModelConnection: vi.fn(),
  deleteModelConnection: vi.fn(),
}));
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listModelConnections).mockResolvedValue({ connections: [], can_manage: true });
  vi.mocked(api.fetchConnectionModels).mockResolvedValue({
    ok: true,
    model_ids: ["test-speech", "test-cleanup"],
    error_kind: null,
  });
  vi.mocked(api.saveModelConnection).mockResolvedValue({} as api.ModelConnection);
});
it("fetches a catalogue with the draft key, saves its model mapping and clears the form", async () => {
  render(<ModelConnectionsSettings />);
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "Add provider" }));
  fireEvent.change(screen.getByLabelText("Provider ID"), { target: { value: "example" } });
  fireEvent.change(screen.getByLabelText("Endpoint"), {
    target: { value: "https://provider.example/v1" },
  });
  fireEvent.change(screen.getByLabelText("API key"), { target: { value: "fake-test-key" } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Fetch models" })));
  expect(screen.getByLabelText("Models")).toHaveValue("test-speech\ntest-cleanup");
  expect(api.fetchConnectionModels).toHaveBeenCalledWith(
    "example",
    expect.objectContaining({ key: { value: "fake-test-key" } }),
  );
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save provider" })));
  expect(api.saveModelConnection).toHaveBeenCalledWith(
    "example",
    expect.objectContaining({ models: { "model-0": "test-speech", "model-1": "test-cleanup" } }),
  );
  expect(screen.queryByLabelText("API key")).toBeNull();
  expect(screen.getByRole("status")).toHaveTextContent("Provider saved");
});
it("shows members the catalogue without admin controls", async () => {
  vi.mocked(api.listModelConnections).mockResolvedValue({ connections: [], can_manage: false });
  render(<ModelConnectionsSettings />);
  await act(async () => {});
  expect(screen.queryByRole("button", { name: "Add provider" })).toBeNull();
});

it("fetches and saves models directly from an existing provider entry", async () => {
  vi.mocked(api.listModelConnections).mockResolvedValue({
    can_manage: true,
    connections: [
      {
        name: "example",
        display_name: "Example",
        kind: "key",
        base_url: "https://provider.example/v1",
        wire_api: "chat",
        models: [],
        key_set: true,
        key_source: "stored",
        editable: true,
      },
    ],
  });
  render(<ModelConnectionsSettings />);
  await act(async () => {});
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Fetch models" })));
  expect(api.fetchConnectionModels).toHaveBeenCalledWith(
    "example",
    expect.not.objectContaining({ key: expect.anything() }),
  );
  expect(api.saveModelConnection).toHaveBeenCalledWith(
    "example",
    expect.objectContaining({ models: { "model-0": "test-speech", "model-1": "test-cleanup" } }),
  );
});
