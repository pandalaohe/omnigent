import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { ModelConnectionsSettings } from "./ModelConnectionsSettings";
import type * as api from "@/lib/modelConnectionsApi";

vi.mock("@/lib/modelConnectionsApi", async (original) => ({
  ...(await original<typeof api>()),
  listModelConnections: vi.fn(async () => ({ connections: [], can_manage: false })),
}));
afterEach(cleanup);
it("uses the dedicated administrator page gate", async () => {
  render(
    <MemoryRouter>
      <ModelConnectionsSettings />
    </MemoryRouter>,
  );
  expect(await screen.findByText("Administrator access required.")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Add provider" })).toBeNull();
});
