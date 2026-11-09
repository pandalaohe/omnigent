import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { BrowserAccessControl } from "./BrowserAccessControl";

const access = { level: "agent", blocked: [], ask: [], allowed: [] };
const bridge = { get: vi.fn().mockResolvedValue({ ok: true, access }), set: vi.fn() };
vi.mock("@/lib/nativeBridge", () => ({ browserAccessBridge: () => bridge }));
describe("browser access settings", () => {
  it("sends edited lists and shows a cancelled native confirmation beside Save", async () => {
    bridge.set.mockResolvedValueOnce({ ok: false, error: "Browser access change cancelled" });
    render(<BrowserAccessControl />);
    await waitFor(() => expect(screen.getByRole("button")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Open without asking"), {
      target: { value: "*.example.com\n127.0.0.0/8" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save browser access" }));
    await waitFor(() =>
      expect(bridge.set).toHaveBeenCalledWith({
        ...access,
        allowed: ["*.example.com", "127.0.0.0/8"],
      }),
    );
    expect(await screen.findByText("Browser access change cancelled")).toBeInTheDocument();
  });
});
