import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { UsageSourceConnect } from "./UsageSourceConnect";
import { UsageTile } from "./UsageTile";
import { usageBoardData } from "./usageBoardFixtures";
import { CONNECT_SOURCES, buildConnectCommand, connectCommandPayload } from "./usageBoardConnect";
import { sumUsage } from "./usageAmountData";
import { sampleTotals, usageActivityFixture } from "./usageActivityFixtures";
import { t } from "./usageText";

afterEach(cleanup);

describe("source connection", () => {
  it("selects each source and shows its supported route or native instructions", () => {
    render(<UsageSourceConnect />);
    for (const source of CONNECT_SOURCES) {
      fireEvent.change(screen.getByRole("combobox"), { target: { value: t(source) } });
      fireEvent.click(screen.getByRole("option", { name: t(source) }));
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

  it("uses the effective credential requirement for pushes on one machine", () => {
    const local = { ...FALLBACK_SERVER_INFO, single_user: true, push_credentials_required: false };
    const view = render(
      <CapabilitiesProvider info={local}>
        <UsageSourceConnect initialSource="report" />
      </CapabilitiesProvider>,
    );
    const command = document.querySelector("pre")?.textContent ?? "";
    expect(command).not.toContain("Authorization: Bearer");
    expect(connectCommandPayload(command, 1794000000)).toBeTruthy();
    expect(screen.getByText(/no api key is needed on this machine/i)).toBeVisible();
    expect(screen.queryByText(/Settings → API keys/)).toBeNull();

    fireEvent.change(screen.getByRole("combobox"), { target: { value: t("otel") } });
    fireEvent.click(screen.getByRole("option", { name: t("otel") }));
    expect(document.querySelector("pre")?.textContent ?? "").not.toContain("Authorization: Bearer");
    expect(screen.queryByText(/Authorization: Bearer \$USAGE_KEY/)).toBeNull();

    for (const auth of [
      { accounts_enabled: true, login_url: "/login" },
      { accounts_enabled: false, login_url: "/auth/login" },
    ]) {
      const info = { ...local, ...auth, push_credentials_required: true };
      view.rerender(
        <CapabilitiesProvider info={info}>
          <UsageSourceConnect initialSource="report" />
        </CapabilitiesProvider>,
      );
      expect(document.querySelector("pre")?.textContent ?? "").toContain(
        "Authorization: Bearer $USAGE_KEY",
      );
      expect(
        screen.getByText(
          auth.accounts_enabled ? /Settings → API keys/ : /a bearer token this server accepts/i,
        ),
      ).toBeVisible();
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
    for (const [density, linkName] of [
      ["std", "Codex Balance"],
      ["mini", "Codex"],
    ] as const) {
      const view = render(
        <QueryClientProvider
          client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
        >
          <MemoryRouter>
            <TooltipProvider>
              <UsageTile
                account={account}
                amount={amount}
                density={density}
                pinnedWindowId={undefined}
                renewal={null}
                onOpen={open}
              />
            </TooltipProvider>
          </MemoryRouter>
        </QueryClientProvider>,
      );
      expect(document.querySelector("[data-usage-amount]")?.textContent).toBe("$0.00");
      expect(screen.getByText("Incomplete coverage")).toBeTruthy();
      expect(screen.getByText("Balance reading is old")).toBeTruthy();
      expect(screen.queryByTestId("usage-ring")).toBeNull();
      fireEvent.click(screen.getByRole("link", { name: linkName }));
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
