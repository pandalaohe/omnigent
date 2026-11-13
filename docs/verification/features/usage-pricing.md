# Purchase baseline API

Use an isolated verification server with Usage enabled and an ordinary authenticated
owner. Use a disposable account report with complete, disjoint token partitions.
The compact settings UI is a separate consumer of these endpoints.

1. GET `/v1/me/usage/pricing-baseline`. Expect defaults, retained cards, revision and
   preview, with `Cache-Control: private, no-store`.
2. PUT the same path with `revision`, `defaults` and `cards`. Monetary inputs are
   base-10 strings. A card needs id/name, retail_amount, purchase_currency,
   allowance_usd, period_kind, starts_on, timezone and an owner-known account or
   proven pool target. A stale revision returns 409; invalid fields return 422.
3. GET `/v1/me/usage/analysis` with the same account/range selection. Inspect
   `pricing`: API value, allocated paid, payback, unit cost, resets and coverage.
   Optional `pool`, `model`, `card` restrict its pricing projection. Shared card
   fees are not split by model; card mode leaves API value/tokens unknown.
4. For a week card priced at HKD100, USD100 allowance, discount0.8 and configured
   HKD→USD0.128205128205128205, report 10M uncached input, 5M output, 20M cached
   input and 2M cache creation with rates USD2/4/0.2/2.5 per million. A full-period
   read yields API USD49.00, paid USD10.26, payback4.78 and USD0.28/M.
   Change the used card override to0.6: paid becomes USD7.69 and payback6.37.
5. Remove HKD conversion: original paid remains available and the converted total
   and dependent ratios become Unknown. Changing display currency clears old FX.
6. Submit real pre-reset windows with `polling_seconds` to retain closing evidence.
   Analysis `quota_closings` returns observation time and bounds; absent polling
   cadence or stale evidence remains Unknown. Editing prices leaves it unchanged.
7. Set the account class to subscription and enter a repeating monthly card with
   no allowance. Use a January 31 start and inspect February and March: renewal
   follows the original day, clamped only in shorter months. Future periods do
   not appear as paid. Cancel before a later renewal or replace the card: earlier
   periods retain their payment, and no new period starts at or after that
   boundary. The detail page shows the next renewal while renewal remains active.
8. Edit the existing repeating card's amount or inherited discount. Before saving,
   inspect the recomputed past periods. To change only future fees, end that card
   and start another at the chosen boundary instead. Refresh after saving and
   confirm the retained cards and revision. A repeating card on a non-subscription
   account must be refused, as must a one-off card without a positive allowance.
9. Compare subscription, prepaid, metered, free and unset accounts in settings,
   tile, detail and Analysis. Subscription/prepaid show API value, paid and
   payback; metered shows spend without payback; free shows usage and quota
   without money. Unset money remains Unknown with a pricing action. An old
   Monthly fee prefills a draft only: cancel the draft and refresh to verify no
   payment was created. Money has two decimal places throughout.

Run the focused pricing math, route, store, quota-closing and migration tests
through the repository's isolated test entry. Database verification covers all
three supported server backends; account revocation includes the new tables.
Record the candidate file hashes, backend versions, outcomes and cleanup proof.
Stop the isolated instance through its own recorded stop route when finished.
