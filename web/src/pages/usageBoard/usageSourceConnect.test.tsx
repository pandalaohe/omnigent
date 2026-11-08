import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { UsageSourceConnect } from "./UsageSourceConnect";
import { UsageTile } from "./UsageTile";
import { usageBoardData } from "./usageBoardFixtures";
import { CONNECT_SOURCES, buildConnectCommand, connectCommandPayload } from "./usageBoardConnect";
import { sumUsage } from "./usageAmounts";
import { sampleTotals, usageActivityFixture } from "./usageActivityFixtures";

afterEach(cleanup);

describe("source connection", () => {
  it("selects each source and shows its supported route or native instructions", () => {
    render(<UsageSourceConnect />);
    for (const source of CONNECT_SOURCES) {
      fireEvent.change(screen.getByRole("combobox"), { target: { value: source } });
      if (source === "subscription")
        expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
      else {
        expect(screen.getByRole("button", { name: "Copy" })).toBeTruthy();
        const command = buildConnectCommand(window.location.origin, source);
        expect(document.querySelector("pre")?.textContent).toBe(command);
        expect(connectCommandPayload(command, 1794000000)).toBeTruthy();
      }
    }
  });

  it("shows a zero balance and its own old reading with no pace, in both densities", () => {
    const account = {
      ...usageBoardData().accounts[0],
      windows: [],
      freshness: "fresh" as const,
      balances: [{ id: "wallet", remaining: 0, unit: "USD", note: "Prepaid" }],
      balance_read_at: 1794000000,
      balance_freshness: "old" as const,
    };
    const open = vi.fn();
    const amount = usageActivityFixture().accounts[0];
    amount.totals.partial_rows = 1;
    for (const density of ["std", "mini"] as const) {
      const view = render(
        <TooltipProvider>
          <UsageTile
            account={account}
            amount={amount}
            density={density}
            pinnedWindowId={undefined}
            renewal={null}
            onOpen={open}
          />
        </TooltipProvider>,
      );
      expect(screen.getByText("0 USD")).toBeTruthy();
      expect(screen.getByText("Incomplete coverage")).toBeTruthy();
      expect(screen.getByText("Balance reading is old")).toBeTruthy();
      expect(screen.queryByTestId("usage-ring")).toBeNull();
      fireEvent.click(screen.getByRole("link"));
      view.unmount();
    }
    expect(open).toHaveBeenCalledTimes(2);
  });
});

it("preserves incomplete coverage when totals are combined", () => {
  expect(
    sumUsage([sampleTotals(2, 20, { partial_rows: 2 }), sampleTotals(4, 40, { partial_rows: 1 })])
      .partial_rows,
  ).toBe(3);
});
