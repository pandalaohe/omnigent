import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiKeysSettings } from "./ApiKeysSettings";
import { apiKeysRequest } from "@/lib/apiKeysApi";
import { settingsNavGroups } from "@/shell/settingsNav";

const identity = vi.hoisted(() => ({ user: "owner", server: "server", generation: 1 }));

vi.mock("@/lib/apiKeysApi", () => ({ apiKeysRequest: vi.fn() }));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => identity.user }));
vi.mock("@/lib/identity", () => ({ getCurrentUserId: () => identity.user }));
vi.mock("@/lib/host", () => ({
  getOmnigentServerIdentity: () => identity.server,
  getOmnigentHostGeneration: () => identity.generation,
  getEmbedRoot: () => null,
}));

const key = {
  id: "one",
  name: "feed",
  prefix: "omk_example",
  scopes: ["*"],
  created_at: 1,
  expires_at: null,
  last_used_at: null,
  regenerated_at: null,
};
const list = { keys: [], read_only: [], available_scopes: ["usage", "tasks"] };
const request = vi.mocked(apiKeysRequest);
afterEach(cleanup);
beforeEach(() => {
  Object.assign(identity, { user: "owner", server: "server", generation: 1 });
  request.mockReset();
  request.mockResolvedValue(list);
});

describe("API key settings", () => {
  it.each([
    ["create", "user"],
    ["create", "server"],
    ["create", "generation"],
    ["create", "unmount"],
    ["regenerate", "user"],
    ["regenerate", "server"],
    ["regenerate", "generation"],
    ["regenerate", "unmount"],
  ])("drops a late %s response after %s changes", async (operation, transition) => {
    let finish!: (result: { key: typeof key; value: string }) => void;
    const pending = new Promise<{ key: typeof key; value: string }>((resolve) => {
      finish = resolve;
    });
    request.mockImplementation(async (_path, method) =>
      method === "POST" ? pending : { ...list, keys: operation === "regenerate" ? [key] : [] },
    );
    const view = render(<ApiKeysSettings />);
    const button = await screen.findByRole("button", {
      name: operation === "create" ? "New API key" : "Regenerate",
    });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    if (operation === "create")
      fireEvent.change(screen.getByLabelText("Name"), { target: { value: "feed" } });
    fireEvent.click(
      screen.getByRole("button", {
        name: operation === "create" ? "Create API key" : "Regenerate API key",
      }),
    );
    await waitFor(() => expect(request.mock.calls.some((call) => call[1] === "POST")).toBe(true));
    if (transition === "unmount") view.unmount();
    else {
      if (transition === "user") identity.user = "other";
      else if (transition === "server") identity.server = "another-server";
      else identity.generation += 1;
      view.rerender(<ApiKeysSettings />);
    }
    await act(async () => {
      finish({ key, value: "late-private-value" });
      await pending;
    });
    expect(screen.queryByText("late-private-value")).not.toBeInTheDocument();
    if (transition === "unmount") {
      render(<ApiKeysSettings />);
      expect(await screen.findByRole("button", { name: "New API key" })).toBeInTheDocument();
      expect(screen.queryByText("late-private-value")).not.toBeInTheDocument();
    }
  });
  it("deletes an expired key without validating its old expiry", async () => {
    const expired = { ...list, keys: [{ ...key, expires_at: 1 }] };
    request.mockImplementation(async (_path, method) =>
      method === "DELETE"
        ? undefined
        : request.mock.calls.some((call) => call[1] === "DELETE")
          ? list
          : expired,
    );
    render(<ApiKeysSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete API key" }));
    await waitFor(() => expect(request).toHaveBeenCalledWith("/one", "DELETE"));
    expect(await screen.findByText("No API keys yet.")).toBeInTheDocument();
    expect(screen.queryByText("Choose a future UTC date")).not.toBeInTheDocument();
  });
  it("reveals once and clears on Done; saves selected scopes and UTC custom date", async () => {
    request.mockImplementation(async (_path, method) =>
      method === "POST" ? { key, value: "synthetic-one-time-value" } : list,
    );
    render(<ApiKeysSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "New API key" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "feed" } });
    fireEvent.click(within(dialog).getByLabelText("Usage"));
    expect(within(dialog).getByLabelText(/All permissions/)).not.toBeChecked();
    fireEvent.change(within(dialog).getByLabelText("Expiration (UTC)"), {
      target: { value: "custom" },
    });
    fireEvent.change(within(dialog).getByLabelText("Custom date"), {
      target: { value: "2027-03-01" },
    });
    expect(within(dialog).getByText("2027-03-01 00:00 UTC")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Create API key" }));
    expect(await screen.findByText("synthetic-one-time-value")).toBeInTheDocument();
    expect(request).toHaveBeenCalledWith("", "POST", {
      name: "feed",
      scopes: ["tasks"],
      expires_at: 1803859200,
    });
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() =>
      expect(screen.queryByText("synthetic-one-time-value")).not.toBeInTheDocument(),
    );
  });

  it("selecting All grants future permissions; expired and read-only entries are clear", async () => {
    request.mockResolvedValue({
      ...list,
      keys: [{ ...key, expires_at: 1 }],
      read_only: [
        { name: "old-server-key", scopes: ["usage"], source: "environment", last_used_at: null },
      ],
    });
    render(<ApiKeysSettings />);
    expect(await screen.findByText(/Expired 1970/)).toBeInTheDocument();
    expect(screen.getByText("Read-only")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Delete" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByLabelText("Usage"));
    fireEvent.click(screen.getByLabelText(/All permissions/));
    expect(screen.getByLabelText("Usage")).toBeChecked();
    expect(screen.getByLabelText("Task board")).toBeChecked();
  });

  it("hides the navigation when unavailable", () => {
    const items = (available: boolean) =>
      settingsNavGroups(true, false, false, false, false, false, false, available).flatMap(
        (group) => group.items,
      );
    expect(items(false).some((item) => item.id === "api-keys")).toBe(false);
    expect(
      items(true)
        .slice(0, 2)
        .map((item) => item.id),
    ).toEqual(["account", "api-keys"]);
  });
});
