import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { UsagePricingPanel } from "./UsagePricingPanel";
import { UsageAccountMoney } from "./UsageAccountMoney";
import { usageBoardData } from "./usageBoardFixtures";
import {
  decimalDisplay,
  multiplyDecimal,
  validatePricing,
  type PricingBaseline,
} from "./usagePricingData";

const account = usageBoardData().accounts[0];
function baseline(): PricingBaseline {
  return {
    revision: 1,
    defaults: {
      schema_version: 1,
      default_discount: "0.8",
      display_currency: "USD",
      fx_to_display: { USD: "1", HKD: "0.128205128205128205" },
    },
    cards: [
      {
        id: "card-a",
        name: "Used card",
        retail_amount: "100.000000000000000001",
        purchase_currency: "HKD",
        allowance_usd: "100",
        period_kind: "week",
        starts_on: "2026-11-02",
        timezone: "UTC",
        target_kind: "account",
        target_id: account.id,
        parent_account_id: null,
        rates_usd_per_million: { read: "2", write: "4", cache_read: "0.2", cache_write: "2.5" },
        discount_override: null,
      },
    ],
    price_overrides: [],
    current_prices: {
      "model-a": {
        imported: { read: "2", write: "4", cache_read: "0.2", cache_write: "2.5" },
        builtin: null,
      },
    },
  };
}
let saved: PricingBaseline;
let writes: PricingBaseline[];
let fail = 0;
beforeEach(() => {
  saved = baseline();
  writes = [];
  fail = 0;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url.includes("pricing-baseline/model-price"))
      return new Response(JSON.stringify({ imported: null, builtin: null }));
    if (url.includes("pricing-baseline")) {
      if (init?.method === "POST")
        return new Response(
          JSON.stringify({
            ...JSON.parse(init.body as string),
            history: [
              {
                card_id: "card-a",
                periods: 3,
                starts_on: "2026-11-02",
                payment: { currency: "HKD", low: "80" },
                total: { currency: "HKD", low: "240" },
              },
            ],
          }),
        );
      if (init?.method === "PUT") {
        const body = JSON.parse(init.body as string) as PricingBaseline;
        writes.push(body);
        if (fail)
          return new Response(
            JSON.stringify({
              detail:
                fail === 409
                  ? "stale revision; refresh and reapply draft"
                  : [{ loc: ["body", "cards", 0, "retail_amount"], msg: "Invalid retail amount" }],
            }),
            { status: fail },
          );
        saved = { ...body, revision: body.revision + 1, current_prices: saved.current_prices };
      }
      return new Response(JSON.stringify(saved));
    }
    return new Response(
      JSON.stringify({
        pool_catalog: [
          { parent_account_id: account.id, pool_id: "source:pool-a", display_name: "Pool A" },
        ],
      }),
    );
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
function panel(focus?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/account/settings#usage-pricing"]}>
        <UsagePricingPanel accounts={[account]} accountId={account.id} focus={focus} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
async function ready() {
  await screen.findByDisplayValue("Used card");
}
function change(label: string, value: string) {
  const input = screen.getByLabelText(label);
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value } });
  fireEvent.blur(input);
}

describe("Pricing settings", () => {
  it("saves nullable account and card fields, shows grey current prices and preserves explicit zero", async () => {
    panel();
    await ready();
    change("Model id", "model-a");
    fireEvent.click(screen.getByRole("button", { name: "Add model price" }));
    expect(screen.getByLabelText("model-a · Read")).toHaveAttribute("placeholder", "2.00");
    expect(screen.getByLabelText("model-a · Read")).toHaveAttribute(
      "title",
      "Current imported price: USD 2.00 / 1M",
    );
    change("model-a · Read", "3.000000000000000001");
    change("Rate model (optional)", "model-a");
    change("Read", "");
    expect(screen.getByLabelText("Read")).toHaveAttribute("placeholder", "3.00");
    expect(screen.getByLabelText("Read")).toHaveAttribute("title", "Account price: USD 3.00 / 1M");
    change("Write", "0");
    fireEvent.click(screen.getByRole("button", { name: "Save pricing" }));
    await screen.findByText("Pricing saved. History recalculated.");
    expect(writes[0].price_overrides?.[0]).toEqual({
      account_id: account.id,
      model: "model-a",
      read: "3.000000000000000001",
      write: null,
      cache_read: null,
      cache_write: null,
    });
    expect(writes[0].cards[0].rates_usd_per_million).toMatchObject({ read: null, write: "0" });
    change("model-a · Read", "");
    fireEvent.click(screen.getByRole("button", { name: "Save pricing" }));
    await waitFor(() => expect(writes).toHaveLength(2));
    expect(writes[1].price_overrides?.[0].read).toBeNull();
    expect(screen.getByLabelText("model-a · Read")).toHaveAttribute("placeholder", "2.00");
  });
  it("retains overrides missing from the import and reapplies them without losing concurrent model prices", async () => {
    saved.price_overrides = [
      {
        account_id: account.id,
        model: "missing-model",
        read: "8",
        write: null,
        cache_read: null,
        cache_write: null,
      },
    ];
    saved.current_prices = { "missing-model": { imported: null, builtin: null } };
    panel();
    await ready();
    expect(screen.getByText(/This model is missing from the imported list/)).toBeTruthy();
    change("missing-model · Read", "9");
    fail = 409;
    fireEvent.click(screen.getByRole("button", { name: "Save pricing" }));
    await screen.findByText("stale revision; refresh and reapply draft");
    saved = {
      ...saved,
      revision: 2,
      price_overrides: [
        ...saved.price_overrides!,
        {
          account_id: account.id,
          model: "concurrent-model",
          read: "1",
          write: null,
          cache_read: null,
          cache_write: null,
        },
      ],
    };
    fireEvent.click(screen.getByRole("button", { name: "Refresh and reapply draft" }));
    await screen.findByText("Draft reapplied to the latest revision. Review and save.");
    fail = 0;
    fireEvent.click(screen.getByRole("button", { name: "Save pricing" }));
    await screen.findByText("Pricing saved. History recalculated.");
    expect(writes[1].price_overrides?.map((row) => [row.model, row.read])).toEqual([
      ["missing-model", "9"],
      ["concurrent-model", "1"],
    ]);
  });
  it("previews replacement discounts and clearing restores inheritance without rounding stored inputs", async () => {
    panel();
    await ready();
    expect(screen.getByLabelText("Card payment preview").textContent).toContain(
      "HKD 80.00 · USD 10.26",
    );
    expect(screen.getByLabelText("Retail price")).toHaveProperty("value", "100.00");
    fireEvent.focus(screen.getByLabelText("Retail price"));
    expect(screen.getByLabelText("Retail price")).toHaveProperty("value", "100.000000000000000001");
    fireEvent.blur(screen.getByLabelText("Retail price"));
    change("Card discount", "0.6");
    expect(screen.getByLabelText("Card payment preview").textContent).toContain(
      "HKD 60.00 · USD 7.69",
    );
    change("Card discount", "");
    change("Default discount", "0.5");
    expect(screen.getByLabelText("Card payment preview").textContent).toContain("HKD 50.00");
    fireEvent.click(screen.getByRole("button", { name: "Save pricing" }));
    await screen.findByText("Pricing saved. History recalculated.");
    expect(writes[0].cards[0].retail_amount).toBe("100.000000000000000001");
    expect(writes[0].cards[0].discount_override).toBeNull();
    expect(writes[0].defaults.fx_to_display.HKD).toBe("0.128205128205128205");
    expect(writes[0].cards[0].rates_usd_per_million?.cache_read).toBe("0.2");
  });
  it("retains a rejected draft and maps errors beside the offending field", async () => {
    panel();
    await ready();
    change("Retail price", "120.125");
    fail = 422;
    fireEvent.click(screen.getByRole("button", { name: "Save pricing" }));
    await screen.findByText("Invalid retail amount");
    expect(screen.getByLabelText("Retail price")).toHaveAttribute("aria-invalid", "true");
    fireEvent.focus(screen.getByLabelText("Retail price"));
    expect(screen.getByLabelText("Retail price")).toHaveProperty("value", "120.125");
    expect(saved.cards[0].retail_amount).toBe("100.000000000000000001");
    fireEvent.click(screen.getByRole("button", { name: "Cancel pricing" }));
    fireEvent.blur(screen.getByLabelText("Retail price"));
    expect(screen.getByLabelText("Retail price")).toHaveProperty("value", "100.00");
  });
  it("refreshes a stale revision and reapplies edits without losing concurrently added cards", async () => {
    panel();
    await ready();
    change("Card discount", "0.6");
    fail = 409;
    fireEvent.click(screen.getByRole("button", { name: "Save pricing" }));
    await screen.findByText("stale revision; refresh and reapply draft");
    saved = {
      ...saved,
      revision: 2,
      cards: [
        ...saved.cards,
        { ...saved.cards[0], id: "card-b", name: "Concurrent card", starts_on: "2026-11-09" },
      ],
    };
    fireEvent.click(screen.getByRole("button", { name: "Refresh and reapply draft" }));
    await screen.findByText("Draft reapplied to the latest revision. Review and save.");
    expect(screen.getByLabelText("Card discount")).toHaveProperty("value", "0.6");
    fail = 0;
    fireEvent.click(screen.getByRole("button", { name: "Save pricing" }));
    await screen.findByText("Pricing saved. History recalculated.");
    expect(writes[1].revision).toBe(2);
    expect(writes[1].cards.map((card) => card.id)).toEqual(["card-a", "card-b"]);
  });
  it("creates a CNY card preselected to a proven pool and preserves copied purchase history", async () => {
    panel("Pool A");
    await ready();
    await waitFor(() =>
      expect(screen.getByLabelText("Account or pool").querySelectorAll("option").length).toBe(2),
    );
    fireEvent.click(screen.getByRole("button", { name: "Add card" }));
    expect(screen.getByLabelText("Purchase currency")).toHaveProperty("value", "CNY");
    expect(screen.getByLabelText("Account or pool")).toHaveProperty(
      "value",
      JSON.stringify(["pool", "source:pool-a", account.id]),
    );
    expect(screen.getByLabelText("Purchased card").querySelectorAll("option")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Cancel pricing" }));
    expect(screen.getByLabelText("Purchased card").querySelectorAll("option")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Copy card" }));
    expect(screen.getByLabelText("Card name")).toHaveProperty("value", "Used card copy");
    expect(screen.getByLabelText("Starts on")).toHaveProperty("value", "2026-11-02");
  });
  it("shows missing conversions as unknown and blocks invalid decimals without silently truncating", async () => {
    panel();
    await ready();
    change("1 HKD = USD", "");
    expect(screen.getByLabelText("Card payment preview").textContent).toContain(
      "HKD 80.00 · USD Unknown",
    );
    change("Card discount", "0");
    expect(screen.getByLabelText("Card payment preview").textContent).toContain("HKD 0.00");
    change("Retail price", "1e3");
    fireEvent.click(screen.getByRole("button", { name: "Save pricing" }));
    expect(writes).toHaveLength(0);
    expect(screen.getByLabelText("Retail price")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Check the highlighted fields before saving.")).toBeTruthy();
  });
  it("requires display-currency save before new FX rates, matching the server reset contract", async () => {
    panel();
    await ready();
    fireEvent.change(screen.getByLabelText("Display currency"), { target: { value: "HKD" } });
    expect(screen.getByLabelText("1 USD = HKD")).toBeDisabled();
    expect(screen.getByText(/Save the display currency first/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save pricing" }));
    await screen.findByText("Pricing saved. History recalculated.");
    expect(writes[0].defaults.fx_to_display).toEqual({ HKD: "1" });
    expect(screen.getByLabelText("1 USD = HKD")).not.toBeDisabled();
  });
  it("previews past subscription periods before saving and invalidates preview after editing", async () => {
    saved.account_classes = { [account.id]: "subscription" };
    panel();
    await ready();
    fireEvent.click(screen.getByLabelText("Automatic renewal"));
    change("USD allowance per reset", "");
    fireEvent.click(screen.getByRole("button", { name: "Save pricing" }));
    await screen.findByLabelText("Recomputed past periods");
    expect(writes).toHaveLength(0);
    expect(screen.getByLabelText("Recomputed past periods").textContent).toContain("3 periods");
    change("Retail price", "120");
    expect(screen.queryByLabelText("Recomputed past periods")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save pricing" }));
    await screen.findByLabelText("Recomputed past periods");
    fireEvent.click(screen.getByRole("button", { name: "Save pricing" }));
    await screen.findByText("Pricing saved. History recalculated.");
    expect(writes[0].cards[0].repeats).toBe(true);
    expect(writes[0].cards[0].allowance_usd).toBeNull();
  });
  it("requires a new history preview after refreshing a conflicting subscription draft", async () => {
    saved.account_classes = { [account.id]: "subscription" };
    saved.cards[0].repeats = true;
    panel();
    await ready();
    change("Retail price", "120");
    fireEvent.click(screen.getByRole("button", { name: "Save pricing" }));
    await screen.findByLabelText("Recomputed past periods");
    fail = 409;
    fireEvent.click(screen.getByRole("button", { name: "Save pricing" }));
    await screen.findByText("stale revision; refresh and reapply draft");
    saved = { ...saved, revision: 2, defaults: { ...saved.defaults, default_discount: "0.4" } };
    fireEvent.click(screen.getByRole("button", { name: "Refresh and reapply draft" }));
    await screen.findByText("Draft reapplied to the latest revision. Review and save.");
    expect(screen.queryByLabelText("Recomputed past periods")).toBeNull();
    fail = 0;
    fireEvent.click(screen.getByRole("button", { name: "Save pricing" }));
    await screen.findByLabelText("Recomputed past periods");
    expect(writes).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Save pricing" }));
    await screen.findByText("Pricing saved. History recalculated.");
    expect(writes[1].defaults.default_discount).toBe("0.4");
  });
});

describe("Decimal inputs", () => {
  it("rounds only at display, keeps half-even and calculates huge decimal products exactly", () => {
    expect(decimalDisplay("1.005")).toBe("1.00");
    expect(decimalDisplay("1.015")).toBe("1.02");
    expect(multiplyDecimal("99999999999999999999", "0.8")).toBe("79999999999999999999.2");
    expect(decimalDisplay(multiplyDecimal("100", "0.6", "0.128205128205128205"))).toBe("7.69");
    const value = baseline();
    value.cards[0].allowance_usd = "0";
    expect(validatePricing(value)["cards.card-a.allowance_usd"]).toContain("greater than zero");
  });
});

it.each(["subscription", "metered", "free"])("shows money appropriate to %s", async (kind) => {
  const amount = { state: "exact", low: "12.5", high: "12.5", currency: "USD" };
  vi.mocked(globalThis.fetch).mockResolvedValue(
    new Response(
      JSON.stringify({
        pricing: {
          account_money: {
            [account.id]: {
              cost_class: kind,
              show_money: kind !== "free",
              show_payback: kind === "subscription",
              estimated: false,
              api_value: amount,
              paid: amount,
              spend: amount,
              payback: amount,
            },
          },
        },
      }),
    ),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UsageAccountMoney accountId={account.id} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  if (kind === "free") await waitFor(() => expect(screen.queryByText("Set pricing")).toBeNull());
  else {
    await screen.findAllByRole("definition");
    expect(screen.getByText(kind === "metered" ? "Spend" : "At API price")).toBeTruthy();
    if (kind === "metered") expect(screen.queryByText("Payback")).toBeNull();
  }
});
