import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ModelReferenceNotice } from "./ModelReferenceNotice";

const mocks = vi.hoisted(() => ({ check: vi.fn(), replace: vi.fn(), save: vi.fn() }));
vi.mock("@/lib/modelReferencesApi", () => ({
  checkModelReferences: mocks.check,
  replaceModelReferences: mocks.replace,
  saveModelProviderAliases: mocks.save,
}));
vi.mock("@/lib/userPreferencesSync", () => ({
  refreshUserPreferencesFromServer: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("./sessionChoice/choiceInvalidation", () => ({
  refreshChoiceConsumers: vi.fn().mockResolvedValue(undefined),
}));

const healthy = {
  rows: [],
  aliases: { pairs: [] },
  aliases_revision: "saved-revision",
  hosts: [],
  models: ["new/x"],
  host_config_manual: false,
};
const row = {
  kind: "calling_default",
  entity_id: "defaults",
  label: "Calling defaults",
  path: "/host/codex/model",
  saved: "old/x",
  harness: "codex",
  host: "host-a",
  outcome: "substituted",
  resolved: "new/x",
  candidates: [],
  still_listed_on: [],
  manual: false,
};

beforeEach(() => {
  vi.resetAllMocks();
  mocks.check.mockResolvedValue(healthy);
});
afterEach(cleanup);
function mount() {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
        })
      }
    >
      <ModelReferenceNotice />
    </QueryClientProvider>,
  );
}

it("shows no warning when all saved models resolve", async () => {
  mount();
  await waitFor(() => expect(mocks.check).toHaveBeenCalled());
  expect(screen.queryByText("Review saved models")).not.toBeInTheDocument();
  expect(screen.getByText("Provider renames")).toBeInTheDocument();
});

it("replaces a suggested model everywhere and reports partial completion honestly", async () => {
  mocks.check.mockResolvedValue({
    ...healthy,
    rows: [row],
    hosts: [{ id: "host-a", name: "Desktop" }],
  });
  mocks.replace.mockResolvedValue({
    complete: false,
    count: 2,
    conflicts: [{ kind: "project", entity_id: "p" }],
    failed: [],
    remaining: 1,
    aliases: "not_requested",
    committed: [],
  });
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Replace model everywhere" }));
  await waitFor(() =>
    expect(mocks.replace).toHaveBeenCalledWith({ mode: "model", from: "old/x", to: "new/x" }),
  );
  expect(await screen.findByRole("status")).toHaveTextContent(
    "Partly replaced: 2 updated, 1 conflicts",
  );
  expect(screen.getByText("Calling defaults · Desktop")).toBeInTheDocument();
});

it("keeps the missing-model warning visible and requires a replacement choice", async () => {
  mocks.check.mockResolvedValue({
    ...healthy,
    rows: [{ ...row, outcome: "missing", resolved: "old/x" }],
  });
  mount();
  expect(await screen.findByText("Not listed here; tried as unknown.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Replace model everywhere" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Replacement for Calling defaults old/x"), {
    target: { value: "new/x" },
  });
  expect(screen.getByRole("button", { name: "Replace model everywhere" })).toBeEnabled();
});

it("edits provider aliases separately from rewriting stored references", async () => {
  mocks.save.mockResolvedValue({ pairs: [["old", "new"]] });
  mount();
  await waitFor(() => expect(mocks.check).toHaveBeenCalled());
  fireEvent.click(screen.getByText("Provider renames"));
  fireEvent.change(screen.getByLabelText("Previous provider"), { target: { value: "old" } });
  fireEvent.change(screen.getByLabelText("Current provider"), { target: { value: "new" } });
  fireEvent.click(screen.getByRole("button", { name: "Add rename" }));
  fireEvent.click(screen.getByRole("button", { name: "Save renames" }));
  await waitFor(() => expect(mocks.save).toHaveBeenCalledWith([["old", "new"]], "saved-revision"));
  expect(mocks.replace).not.toHaveBeenCalled();
});
