import { nanoid } from "nanoid";
import { cloneElement, useId, useState, type ReactElement } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "@/lib/routing";
import { boardPath } from "@/lib/boardFrame";
import { Button } from "@/components/ui/button";
import { Combobox, COMBOBOX_OPTION_THRESHOLD } from "@/components/ui/combobox";
import { Input } from "@/components/ui/input";
import type { UsageAccountView } from "./usageBoardData";
import { usageJson } from "./usagePriceSourceData";
import { UsageName } from "./UsageName";
import {
  CURRENCIES,
  COST_CLASSES,
  accountCostClass,
  RATE_KEYS,
  PRICING_QUERY_KEY,
  PricingSaveError,
  accountPriceKey,
  decimalDisplay,
  multiplyDecimal,
  reapplyPricing,
  savePricingBaseline,
  usePricingBaseline,
  validatePricing,
  validDecimal,
  sameRate,
  type Currency,
  type CostClass,
  type PricingCard,
  type PricingBaseline,
  type Rates,
  type CurrentModelPrice,
} from "./usagePricingData";

interface Pool {
  parent_account_id: string;
  pool_id: string;
  display_name: string;
}
export function UsagePricingPanel({
  accounts,
  accountId,
  focus,
  monthlyFee,
}: {
  accounts: UsageAccountView[];
  accountId?: string;
  focus?: string | null;
  monthlyFee?: number;
}) {
  const location = useLocation();
  return (
    <details
      id="usage-pricing"
      className="usage-pricing border-t border-border py-3"
      open={["#usage-pricing", "#usage-settings"].includes(location.hash) ? true : undefined}
    >
      <summary className="min-h-8 cursor-pointer text-sm font-semibold">Pricing</summary>
      <PricingEditor
        accounts={accounts}
        accountId={accountId}
        focus={focus}
        monthlyFee={monthlyFee}
      />
    </details>
  );
}

function PricingEditor({
  accounts,
  accountId,
  focus,
  monthlyFee,
}: {
  accounts: UsageAccountView[];
  accountId?: string;
  focus?: string | null;
  monthlyFee?: number;
}) {
  const query = usePricingBaseline();
  const client = useQueryClient();
  const catalog = useQuery({
    queryKey: ["usageBoard", "pricingPools"],
    queryFn: () => usageJson<{ pool_catalog: Pool[] }>("analysis?range=d7"),
    retry: false,
  });
  const [draft, setDraft] = useState<PricingBaseline | null>(null);
  const [original, setOriginal] = useState<PricingBaseline | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState<PricingSaveError | null>(null);
  const [fields, setFields] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<PricingBaseline | null>(null);
  const [previewFor, setPreviewFor] = useState<string | null>(null);
  const baseline = draft ?? query.data;
  if (!baseline)
    return (
      <div className="py-2 text-xs text-muted-foreground">
        {query.error ? (
          <>
            <p role="alert">Pricing could not be read. Your saved cards have not changed.</p>
            <Button size="sm" variant="ghost" onClick={() => void query.refetch()}>
              Retry pricing
            </Button>
          </>
        ) : (
          "Reading pricing…"
        )}
      </div>
    );
  const pools = catalog.data?.pool_catalog ?? [];
  const cards = baseline.cards.filter(
    (card) => !accountId || (card.parent_account_id ?? card.target_id) === accountId,
  );
  const focusedPool = pools.filter(
    (pool) =>
      pool.parent_account_id === accountId &&
      (pool.pool_id === focus || pool.display_name === focus),
  );
  const card =
    cards.find((item) => item.id === selected) ??
    cards.find(
      (item) =>
        item.target_kind === "pool" &&
        focusedPool.length === 1 &&
        item.target_id === focusedPool[0].pool_id,
    ) ??
    cards[0];
  const errors = { ...validatePricing(baseline), ...fields };
  const prefix = card ? `cards.${card.id}.` : "";
  const currencyChanged =
    baseline.defaults.display_currency !== (original ?? query.data)?.defaults.display_currency;
  function edit(next: PricingBaseline) {
    setPreview(null);
    setPreviewFor(null);
    if (!draft && query.data) setOriginal(query.data);
    setDraft(next);
    setFields({});
    setError(null);
    setMessage("");
  }
  function editCard(patch: Partial<PricingCard>) {
    if (baseline && card)
      edit({
        ...baseline,
        cards: baseline.cards.map((item) => (item.id === card.id ? { ...item, ...patch } : item)),
      });
  }
  function newCard(copy = false) {
    if (!baseline) return;
    const parent = accountId ?? accounts[0]?.id ?? "";
    const pool = pools.filter(
      (item) =>
        item.parent_account_id === parent &&
        (item.pool_id === focus || item.display_name === focus),
    );
    const id = `card-${nanoid()}`;
    const next: PricingCard =
      copy && card
        ? { ...structuredClone(card), id, name: `${card.name} copy` }
        : {
            id,
            name: "New card",
            retail_amount: monthlyFee != null ? String(monthlyFee) : "",
            purchase_currency: monthlyFee != null ? "USD" : "CNY",
            allowance_usd: accountCostClass(baseline, parent) === "subscription" ? null : "",
            repeats: accountCostClass(baseline, parent) === "subscription",
            period_kind: "month",
            starts_on: new Date().toLocaleDateString("en-CA"),
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            target_kind: pool.length === 1 ? "pool" : "account",
            target_id: pool.length === 1 ? pool[0].pool_id : parent,
            parent_account_id: pool.length === 1 ? parent : null,
            discount_override: null,
            rates_usd_per_million: null,
          };
    edit({ ...baseline, cards: [...baseline.cards, next] });
    setSelected(id);
  }
  const targets = [
    ...accounts.map((account) => ({
      key: JSON.stringify(["account", account.id, null]),
      label: account.display_name,
    })),
    ...pools.map((pool) => ({
      key: JSON.stringify(["pool", pool.pool_id, pool.parent_account_id]),
      label: `${accounts.find((a) => a.id === pool.parent_account_id)?.display_name ?? pool.parent_account_id} · ${pool.display_name}`,
    })),
  ];
  const target = card
    ? JSON.stringify([card.target_kind, card.target_id, card.parent_account_id])
    : "";
  if (card && !targets.some((item) => item.key === target))
    targets.push({ key: target, label: `${card.target_id} (retained)` });
  const paid = card
    ? multiplyDecimal(
        card.retail_amount,
        card.discount_override ?? baseline.defaults.default_discount,
      )
    : null;
  const rate = card ? baseline.defaults.fx_to_display[card.purchase_currency] : undefined;
  const converted = paid !== null && rate ? multiplyDecimal(paid, rate) : null;
  async function save() {
    if (!baseline) return;
    const invalid = validatePricing(baseline);
    setFields(invalid);
    if (Object.keys(invalid).length) {
      setError(new PricingSaveError("Check the highlighted fields before saving.", 422));
      return;
    }
    setSaving(true);
    setError(null);
    setMessage("");
    try {
      if (
        baseline.cards.some((item) => item.repeats) &&
        (!preview || previewFor !== JSON.stringify(baseline))
      ) {
        setPreview(await savePricingBaseline(baseline, true));
        setPreviewFor(JSON.stringify(baseline));
        setMessage("Review recomputed past periods, then save pricing.");
        return;
      }
      const saved = await savePricingBaseline(baseline);
      client.setQueryData(PRICING_QUERY_KEY, saved);
      setDraft(null);
      setOriginal(null);
      setFields({});
      setPreview(null);
      setMessage("Pricing saved. History recalculated.");
      await client.invalidateQueries({
        predicate: (item) =>
          item.queryKey[0] === "usageBoard" && item.queryKey[1] !== "pricingBaseline",
      });
    } catch (caught) {
      const failure =
        caught instanceof PricingSaveError
          ? caught
          : new PricingSaveError(
              caught instanceof Error ? caught.message : "Could not save pricing.",
              0,
            );
      setError(failure);
      setFields(failure.fields);
    } finally {
      setSaving(false);
    }
  }
  return (
    <fieldset disabled={saving} className="min-w-0 border-0 p-0">
      {(accountId ? accounts.filter((item) => item.id === accountId) : accounts).map((account) => (
        <Field key={account.id} label={`Cost class · ${account.display_name}`}>
          <select
            value={baseline.account_classes?.[account.id] ?? ""}
            onChange={(event) =>
              edit({
                ...baseline,
                account_classes: {
                  ...baseline.account_classes,
                  [account.id]: (event.target.value || null) as CostClass | null,
                },
              })
            }
          >
            <option value="">
              Automatic ·{" "}
              {COST_CLASSES[baseline.account_class_defaults?.[account.id] as CostClass] ?? "Unset"}
            </option>
            {Object.entries(COST_CLASSES).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </Field>
      ))}
      {accountId && accountCostClass(baseline, accountId) === null ? (
        <p className="usage-caption">Set a cost class to show money.</p>
      ) : null}
      <div className="usage-pricing-defaults">
        <DecimalField
          label="Default discount"
          value={baseline.defaults.default_discount}
          money={false}
          error={errors["defaults.default_discount"]}
          onChange={(value) =>
            edit({ ...baseline, defaults: { ...baseline.defaults, default_discount: value } })
          }
        />
        <Field label="Display currency">
          <select
            value={baseline.defaults.display_currency}
            onChange={(event) => {
              const currency = event.target.value as Currency;
              edit({
                ...baseline,
                defaults: {
                  ...baseline.defaults,
                  display_currency: currency,
                  fx_to_display: { [currency]: "1" },
                },
              });
            }}
          >
            {CURRENCIES.map((currency) => (
              <option key={currency}>{currency}</option>
            ))}
          </select>
        </Field>
      </div>
      <details className="text-xs">
        <summary className="min-h-8 cursor-pointer text-muted-foreground">
          Currency conversions
        </summary>
        {currencyChanged ? (
          <p className="py-1 text-muted-foreground">
            Save the display currency first; conversion rates are cleared when it changes.
          </p>
        ) : null}
        <div className="grid gap-2 sm:grid-cols-3">
          {CURRENCIES.map((currency) =>
            currency === baseline.defaults.display_currency ? (
              <p key={currency} className="py-2">
                1 {currency} = 1 {currency}
              </p>
            ) : (
              <DecimalField
                key={currency}
                label={`1 ${currency} = ${baseline.defaults.display_currency}`}
                money={false}
                optional
                disabled={currencyChanged}
                value={baseline.defaults.fx_to_display[currency] ?? ""}
                error={errors[`defaults.fx_to_display.${currency}`]}
                onChange={(value) => {
                  const fx = { ...baseline.defaults.fx_to_display };
                  if (value) fx[currency] = value;
                  else Reflect.deleteProperty(fx, currency);
                  edit({ ...baseline, defaults: { ...baseline.defaults, fx_to_display: fx } });
                }}
              />
            ),
          )}
        </div>
      </details>
      <AccountPriceTable
        accounts={accounts}
        accountId={accountId}
        baseline={baseline}
        errors={errors}
        onChange={edit}
      />
      {!accountId ||
      ["subscription", "prepaid"].includes(accountCostClass(baseline, accountId) ?? "") ||
      cards.length > 0 ? (
        <div className="usage-pricing-card-list">
          <Field label="Purchased card">
            {cards.length > COMBOBOX_OPTION_THRESHOLD ? (
              <Combobox
                mode="select"
                options={cards.map((item) => ({
                  key: item.id,
                  label: `${item.name} · ${item.starts_on}`,
                }))}
                value={card?.id ?? ""}
                onValueChange={setSelected}
                aria-label="Purchased card"
                className="h-auto w-full rounded-none! bg-transparent p-0"
              />
            ) : (
              <select
                value={card?.id ?? ""}
                title={card ? `${card.name} · ${card.starts_on}` : "No saved cards"}
                onChange={(event) => setSelected(event.target.value)}
              >
                {!cards.length ? (
                  <option value="">No saved cards</option>
                ) : (
                  cards.map((item) => (
                    <option
                      value={item.id}
                      key={item.id}
                      title={`${item.name} · ${item.starts_on}`}
                    >
                      {item.name} · {item.starts_on}
                    </option>
                  ))
                )}
              </select>
            )}
          </Field>
          <Button variant="outline" size="sm" onClick={() => newCard()}>
            Add card
          </Button>
          {card ? (
            <Button variant="ghost" size="sm" onClick={() => newCard(true)}>
              Copy card
            </Button>
          ) : null}
        </div>
      ) : (
        <p className="usage-caption">
          {accountId && accountCostClass(baseline, accountId) === "free"
            ? "Free accounts show usage and quota only."
            : "No purchase card required."}
        </p>
      )}
      {card ? (
        <div className="usage-pricing-editor" key={card.id}>
          <div className="usage-pricing-two">
            <Field label="Card name" error={errors[prefix + "name"]}>
              <Input
                value={card.name}
                title={card.name}
                maxLength={128}
                onChange={(event) => editCard({ name: event.target.value })}
              />
            </Field>
            <Field label="Account or pool" error={errors[prefix + "target_id"]}>
              {targets.length > COMBOBOX_OPTION_THRESHOLD ? (
                <Combobox
                  mode="select"
                  options={targets}
                  value={target}
                  onValueChange={(value) => {
                    const [kind, id, parent] = JSON.parse(value) as [
                      PricingCard["target_kind"],
                      string,
                      string | null,
                    ];
                    editCard({ target_kind: kind, target_id: id, parent_account_id: parent });
                  }}
                  aria-label="Account or pool"
                  className="h-auto w-full rounded-none! bg-transparent p-0"
                />
              ) : (
                <select
                  value={target}
                  title={targets.find((item) => item.key === target)?.label ?? target}
                  onChange={(event) => {
                    const [kind, id, parent] = JSON.parse(event.target.value) as [
                      PricingCard["target_kind"],
                      string,
                      string | null,
                    ];
                    editCard({ target_kind: kind, target_id: id, parent_account_id: parent });
                  }}
                >
                  {targets.map((item) => (
                    <option value={item.key} key={item.key} title={item.label}>
                      {item.label}
                    </option>
                  ))}
                </select>
              )}
            </Field>
          </div>
          <div className="usage-pricing-two">
            <DecimalField
              label="Retail price"
              value={card.retail_amount}
              error={errors[prefix + "retail_amount"]}
              onChange={(value) => editCard({ retail_amount: value })}
            />
            <Field label="Purchase currency">
              <select
                value={card.purchase_currency}
                onChange={(event) =>
                  editCard({ purchase_currency: event.target.value as Currency })
                }
              >
                {CURRENCIES.map((currency) => (
                  <option key={currency}>{currency}</option>
                ))}
              </select>
            </Field>
            <DecimalField
              label="USD allowance per reset"
              value={card.allowance_usd ?? ""}
              optional={card.repeats}
              error={errors[prefix + "allowance_usd"]}
              onChange={(value) => editCard({ allowance_usd: value || null })}
            />
            <Field label="Billing period">
              <select
                value={card.period_kind}
                onChange={(event) =>
                  editCard({ period_kind: event.target.value as PricingCard["period_kind"] })
                }
              >
                <option value="week">Week</option>
                <option value="month">Month</option>
                <option value="month_weekly_reset">Month · weekly reset</option>
              </select>
            </Field>
            <Field label="Starts on" error={errors[prefix + "starts_on"]}>
              <Input
                type="date"
                value={card.starts_on}
                onChange={(event) => editCard({ starts_on: event.target.value })}
              />
            </Field>
            <Field label="Timezone" error={errors[prefix + "timezone"]}>
              <Input
                value={card.timezone}
                onChange={(event) => editCard({ timezone: event.target.value })}
              />
            </Field>
          </div>
          {accountCostClass(baseline, card.parent_account_id ?? card.target_id) ===
          "subscription" ? (
            <>
              <label className="flex min-h-9 items-center gap-2">
                <input
                  type="checkbox"
                  checked={card.repeats ?? false}
                  onChange={(event) => editCard({ repeats: event.target.checked, ends_on: null })}
                />
                Automatic renewal
              </label>
              {card.repeats ? (
                <Field label="Ends on (exclusive)" error={errors[prefix + "ends_on"]}>
                  <Input
                    type="date"
                    value={card.ends_on ?? ""}
                    onChange={(event) => editCard({ ends_on: event.target.value || null })}
                  />
                </Field>
              ) : null}
              <Button
                size="sm"
                variant="outline"
                disabled={!card.repeats || !card.ends_on}
                onClick={() => {
                  const id = `card-${nanoid()}`;
                  const next = {
                    ...structuredClone(card),
                    id,
                    name: `${card.name} replacement`,
                    starts_on: card.ends_on!,
                    ends_on: null,
                  };
                  edit({ ...baseline, cards: [...baseline.cards, next] });
                  setSelected(id);
                }}
              >
                End here and start a new card
              </Button>
            </>
          ) : null}
          <DecimalField
            label="Card discount"
            money={false}
            optional
            value={card.discount_override ?? ""}
            placeholder={decimalDisplay(baseline.defaults.default_discount, 3, true)}
            error={errors[prefix + "discount_override"]}
            onChange={(value) => editCard({ discount_override: value || null })}
          />
          <p className="text-xs text-muted-foreground">
            Blank uses the default. A card discount replaces the default; saving recalculates
            history. Used cards remain editable.
          </p>
          <details className="text-xs" open={!!card.rates_usd_per_million}>
            <summary className="min-h-8 cursor-pointer">Token rates · USD / 1M</summary>
            <label className="flex min-h-9 items-center gap-2">
              <input
                type="checkbox"
                checked={!!card.rates_usd_per_million}
                onChange={(event) =>
                  editCard({
                    rates_usd_per_million: event.target.checked
                      ? { read: null, write: null, cache_read: null, cache_write: null }
                      : null,
                  })
                }
              />
              Use card token rates
            </label>
            {card.rates_usd_per_million ? (
              <>
                <Field label="Rate model (optional)">
                  <Input
                    maxLength={128}
                    value={card.rates_usd_per_million.model ?? ""}
                    title={card.rates_usd_per_million.model ?? ""}
                    onChange={(event) =>
                      editCard({
                        rates_usd_per_million: {
                          ...card.rates_usd_per_million!,
                          model: event.target.value || null,
                        },
                      })
                    }
                  />
                </Field>
                <RateInputs
                  baseline={baseline}
                  model={card.rates_usd_per_million.model ?? ""}
                  accountId={card.parent_account_id ?? card.target_id}
                  card
                  rates={card.rates_usd_per_million}
                  errors={errors}
                  prefix={prefix + "rates_usd_per_million."}
                  onChange={(rates) =>
                    editCard({
                      rates_usd_per_million: { ...card.rates_usd_per_million!, ...rates },
                    })
                  }
                />
                <p className="text-muted-foreground">
                  Blank card fields inherit the account price, then the current imported or built-in
                  price. With no model named, set fields apply only when the covered usage has one
                  model.
                </p>
              </>
            ) : null}
          </details>
          <output
            className="block rounded-lg bg-muted/40 px-3 py-2 text-sm"
            aria-label="Card payment preview"
          >
            Full period paid · {card.purchase_currency} {decimalDisplay(paid)}
            {card.purchase_currency !== baseline.defaults.display_currency
              ? ` · ${baseline.defaults.display_currency} ${decimalDisplay(converted)}`
              : ""}
          </output>
          <p className="text-xs text-muted-foreground">
            {card.repeats
              ? "Renewals create payments only when their period starts. Editing this card recalculates past periods."
              : "One purchase only. Weekly resets do not add another payment."}{" "}
            Quota remaining comes from provider readings.
          </p>
        </div>
      ) : (
        <p className="py-2 text-xs text-muted-foreground">
          Add an actual purchase to set its pricing baseline.
        </p>
      )}
      {preview ? (
        <output aria-label="Recomputed past periods" className="block usage-card-note">
          {preview.history?.map((item) => (
            <p key={item.card_id}>
              {baseline.cards.find((purchase) => purchase.id === item.card_id)?.name} ·{" "}
              {item.starts_on} · {item.periods} periods · {item.payment.currency}{" "}
              {decimalDisplay(item.payment.low)} each · {decimalDisplay(item.total.low)} total
            </p>
          ))}
        </output>
      ) : null}
      {error ? (
        <div role="alert" className="py-2 text-xs text-destructive">
          <p>{error.message}</p>
          {error.status === 409 ? (
            <Button
              variant="outline"
              size="sm"
              onClick={async () => {
                try {
                  const latest = await query.refetch();
                  if (latest.error) throw latest.error;
                  if (latest.data && original && draft) {
                    setDraft(reapplyPricing(original, draft, latest.data));
                    setPreview(null);
                    setPreviewFor(null);
                    setOriginal(latest.data);
                    setError(null);
                    setMessage("Draft reapplied to the latest revision. Review and save.");
                  }
                } catch {
                  setError(new PricingSaveError("Could not refresh pricing. Draft retained.", 0));
                }
              }}
            >
              Refresh and reapply draft
            </Button>
          ) : null}
        </div>
      ) : null}
      {query.error && draft ? (
        <p role="alert" className="text-xs text-destructive">
          Pricing refresh failed. Draft retained.
        </p>
      ) : null}
      <p role="status" className="py-1 text-xs text-muted-foreground">
        {message}
      </p>
      <div className="flex gap-2">
        <Button size="sm" disabled={!draft} onClick={() => void save()}>
          {saving ? "Saving…" : "Save pricing"}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          disabled={!draft}
          onClick={() => {
            setDraft(null);
            setOriginal(null);
            setFields({});
            setError(null);
            setMessage("Draft cancelled.");
            setPreview(null);
          }}
        >
          Cancel pricing
        </Button>
      </div>
    </fieldset>
  );
}

const EMPTY_RATES: Rates = { read: null, write: null, cache_read: null, cache_write: null };

function AccountPriceTable({
  accounts,
  accountId,
  baseline,
  errors,
  onChange,
}: {
  accounts: UsageAccountView[];
  accountId?: string;
  baseline: PricingBaseline;
  errors: Record<string, string>;
  onChange: (value: PricingBaseline) => void;
}) {
  const [selectedAccount, setSelectedAccount] = useState("");
  const [newModel, setNewModel] = useState("");
  const parent =
    accountId ??
    (accounts.some((account) => account.id === selectedAccount)
      ? selectedAccount
      : (accounts[0]?.id ?? ""));
  const overrides = baseline.price_overrides ?? [];
  const rows = overrides.filter((row) => row.account_id === parent);
  const models = [
    ...new Map(
      [
        ...(accounts.find((account) => account.id === parent)?.models?.map((model) => model.id) ??
          []),
        ...rows.map((row) => row.model),
      ].map((model) => [accountPriceKey(parent, model), model]),
    ).values(),
  ].sort();
  function editModel(model: string, rates: Rates) {
    onChange({
      ...baseline,
      price_overrides: [
        ...overrides.filter(
          (row) => accountPriceKey(row.account_id, row.model) !== accountPriceKey(parent, model),
        ),
        { account_id: parent, model, ...rates },
      ],
    });
  }
  return (
    <section
      className="usage-pricing-editor border-t border-border pt-3"
      aria-label="Account model prices"
    >
      <h3 className="usage-card-title">Account model prices · USD / 1M</h3>
      {!accountId ? (
        <Field label="Price account">
          {accounts.length > COMBOBOX_OPTION_THRESHOLD ? (
            <Combobox
              mode="select"
              options={accounts.map((account) => ({
                key: account.id,
                label: account.display_name,
              }))}
              value={parent}
              onValueChange={setSelectedAccount}
              aria-label="Price account"
              className="h-auto w-full rounded-none! bg-transparent p-0"
            />
          ) : (
            <select value={parent} onChange={(event) => setSelectedAccount(event.target.value)}>
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.display_name}
                </option>
              ))}
            </select>
          )}
        </Field>
      ) : null}
      <div className="usage-pricing-card-list">
        <Field label="Model id">
          <Input
            value={newModel}
            maxLength={128}
            onChange={(event) => setNewModel(event.target.value)}
          />
        </Field>
        <Button
          size="sm"
          variant="outline"
          disabled={
            !parent ||
            !newModel.trim() ||
            models.some(
              (model) =>
                accountPriceKey(parent, model) === accountPriceKey(parent, newModel.trim()),
            )
          }
          onClick={() => {
            editModel(newModel.trim(), EMPTY_RATES);
            setNewModel("");
          }}
        >
          Add model price
        </Button>
      </div>
      {models.map((model) => (
        <fieldset
          className="min-w-0 border-t border-border py-2"
          key={accountPriceKey(parent, model)}
        >
          <legend className="max-w-full break-all text-xs">{model}</legend>
          <RateInputs
            baseline={baseline}
            accountId={parent}
            model={model}
            labelPrefix={`${model} · `}
            rates={
              rows.find(
                (row) => accountPriceKey(parent, row.model) === accountPriceKey(parent, model),
              ) ?? EMPTY_RATES
            }
            errors={errors}
            prefix={`price_overrides.${parent}.${model}.`}
            onChange={(rates) => editModel(model, rates)}
          />
          {errors[`price_overrides.${parent}.${model}.model`] ? (
            <p role="alert" className="text-xs text-destructive">
              {errors[`price_overrides.${parent}.${model}.model`]}
            </p>
          ) : null}
        </fieldset>
      ))}
      {!models.length ? (
        <p className="text-xs text-muted-foreground">Add a model id to override its prices.</p>
      ) : null}
      <p className="text-xs text-muted-foreground">
        Grey values show current inherited prices. Changing any field recalculates this model's
        history using your overrides plus current prices. Clear every override to restore recorded
        valuations; these can differ from today's grey values. Missing imported models keep their
        overrides.
      </p>
    </section>
  );
}

function RateInputs({
  baseline,
  accountId,
  model,
  card = false,
  rates,
  errors,
  prefix,
  labelPrefix = "",
  onChange,
}: {
  baseline: PricingBaseline;
  accountId: string;
  model: string;
  card?: boolean;
  rates: Rates;
  errors: Record<string, string>;
  prefix: string;
  labelPrefix?: string;
  onChange: (rates: Rates) => void;
}) {
  const current = baseline.current_prices?.[model];
  const query = useQuery({
    queryKey: ["usageBoard", "pricingModel", model],
    queryFn: () =>
      usageJson<CurrentModelPrice>(
        `pricing-baseline/model-price?model=${encodeURIComponent(model)}`,
      ),
    enabled: !!model && !current,
    retry: false,
    staleTime: 60_000,
  });
  const prices = current ?? query.data;
  const accountRows = (baseline.price_overrides ?? []).filter(
    (row) => row.account_id === accountId,
  );
  const modelKey = model.toLowerCase(),
    bare = modelKey.split("/").at(-1);
  let account =
    accountRows.find((row) => row.model.toLowerCase() === modelKey) ??
    accountRows.find((row) => row.model.toLowerCase() === bare);
  if (!account) {
    const candidates = accountRows.filter(
      (row) => row.model.toLowerCase().split("/").at(-1) === bare,
    );
    if (
      candidates.length &&
      candidates.every((row) =>
        RATE_KEYS.every((field) => sameRate(row[field], candidates[0][field])),
      )
    )
      account = candidates[0];
  }
  return (
    <>
      <div className="usage-pricing-two">
        {RATE_KEYS.map((key, index) => {
          const inherited =
            (card ? account?.[key] : null) ?? prices?.imported?.[key] ?? prices?.builtin?.[key];
          const layer =
            card && account?.[key] != null
              ? "Account price"
              : prices?.imported?.[key] != null
                ? "Current imported price"
                : "Built-in price";
          return (
            <DecimalField
              key={key}
              label={labelPrefix + ["Read", "Write", "Cache read", "Cache write"][index]}
              value={rates[key] ?? ""}
              optional
              error={errors[prefix + key]}
              placeholder={inherited != null ? decimalDisplay(inherited) : "Unknown"}
              hint={
                inherited != null
                  ? `${layer}: USD ${decimalDisplay(inherited)} / 1M`
                  : "No inherited price available"
              }
              onChange={(value) => onChange({ ...rates, [key]: value || null })}
            />
          );
        })}
      </div>
      {model && prices && !prices.imported ? (
        <p className="text-xs text-muted-foreground">
          This model is missing from the imported list. Overrides are retained; blank fields use
          built-in prices when available.
        </p>
      ) : null}
      {model && query.error ? (
        <p role="alert" className="text-xs text-destructive">
          Inherited prices could not be read. Your draft is retained.
        </p>
      ) : null}
    </>
  );
}

function Field({
  label,
  error,
  children,
}: {
  label: string;
  error?: string;
  children: ReactElement<{ id?: string; "aria-describedby"?: string; "aria-invalid"?: boolean }>;
}) {
  const id = useId();
  return (
    <div className="grid min-w-0 gap-1 text-xs">
      <label htmlFor={id}>{label}</label>
      {cloneElement(children, {
        id,
        "aria-describedby": error ? `${id}-error` : undefined,
        "aria-invalid": !!error,
      })}
      {error ? (
        <span id={`${id}-error`} className="text-destructive">
          {error}
        </span>
      ) : null}
    </div>
  );
}
function DecimalField({
  label,
  value,
  onChange,
  money = true,
  optional = false,
  error,
  disabled = false,
  placeholder,
  hint,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  money?: boolean;
  optional?: boolean;
  error?: string;
  disabled?: boolean;
  placeholder?: string;
  hint?: string;
}) {
  const [editing, setEditing] = useState(false);
  const id = useId();
  return (
    <label className="grid min-w-0 gap-1 text-xs">
      <span>{label}</span>
      <Input
        aria-label={label}
        maxLength={40}
        inputMode="decimal"
        disabled={disabled}
        value={
          editing || !validDecimal(value) ? value : decimalDisplay(value, money ? 2 : 3, !money)
        }
        aria-invalid={!!error}
        aria-describedby={error ? id : undefined}
        placeholder={placeholder ?? (optional ? "Not set" : "")}
        title={hint}
        onFocus={() => setEditing(true)}
        onBlur={() => setEditing(false)}
        onChange={(event) => onChange(event.target.value)}
      />
      {error ? (
        <span id={id} className="text-destructive">
          {error}
        </span>
      ) : null}
    </label>
  );
}

export function UsagePricingSummary({ accountId }: { accountId: string }) {
  const query = usePricingBaseline();
  const cards =
    query.data?.cards.filter((card) => (card.parent_account_id ?? card.target_id) === accountId) ??
    [];
  const kind = query.data ? accountCostClass(query.data, accountId) : null;
  if (kind === "free" || kind === "metered") return null;
  return (
    <section className="usage-card">
      <div className="flex items-center justify-between">
        <h2 className="usage-card-title">Card pricing</h2>
        <Link
          className="inline-flex min-h-11 items-center text-xs underline"
          to={boardPath(`/usage/board/${encodeURIComponent(accountId)}/settings#usage-pricing`)}
        >
          Edit pricing
        </Link>
      </div>
      {cards.length ? (
        cards.map((card) => (
          <div
            key={card.id}
            className="flex min-w-0 flex-wrap items-center justify-between gap-2 border-t border-border py-2 text-xs"
          >
            <UsageName className="flex-1">{`${card.name} · ${card.starts_on}`}</UsageName>
            {card.repeats ? (
              <span className="usage-caption">
                {query.data?.renewals?.[card.id]
                  ? `自动续 · 下次 ${new Date(query.data.renewals[card.id]! * 1000).toLocaleDateString(undefined, { timeZone: card.timezone })}`
                  : "自动续已结束"}
              </span>
            ) : null}
            <span>
              {card.purchase_currency}{" "}
              {decimalDisplay(
                multiplyDecimal(
                  card.retail_amount,
                  card.discount_override ?? query.data!.defaults.default_discount,
                ),
              )}
              {kind === "prepaid" && card.allowance_usd ? (
                <small className="block usage-caption">
                  USD {decimalDisplay(card.allowance_usd)} allowance · paid per USD 1.00 in Analysis
                </small>
              ) : null}
            </span>
          </div>
        ))
      ) : (
        <p className="text-xs text-muted-foreground">
          {query.isPending
            ? "Reading card pricing…"
            : query.error
              ? "Card pricing unavailable"
              : "No pricing baseline"}
        </p>
      )}
    </section>
  );
}
