# Connect a usage source

Open **Settings → Usage board → Connect**. Pick the source type, then copy its
sample. The first successful push creates the account. Omnigent accepts pushes;
it does not poll your vendor or store your vendor password.

## Credentials

In accounts mode, create a key in **Settings → API keys**, keeping Usage enabled.
Copy the value when it is shown, then store it in the integration as `USAGE_KEY`.
The value is shown only once; regenerating a key invalidates its old value immediately.
Permissions can be edited later. Expiration defaults to none; optional dates use UTC.
The database stores only a SHA-256 digest and a display prefix in `user_api_keys`.
Manage or delete the key in Settings. For headless deployments, the config-file
`api_keys` entries below remain supported. The legacy usage token continues working
until its callers have switched; keep their last usage-feed cursor across the switch.

For a config-file key, the operator generates a random value, delivers it over a
private channel and stores its SHA-256 digest in `config.yaml`:

```yaml
api_keys:
  - name: usage-pusher
    user: your-existing-username
    scopes: [usage]
    sha256: <64 lowercase hexadecimal characters>
```

For config-file keys, the user must already exist. Values must be 32–256 characters from
`[A-Za-z0-9_-]`. Generate one outside your checkout in a private directory;
this writes a new private file and prints only the digest for the configuration:

```sh
python - <<'PYKEY'
import hashlib, os, secrets
key = secrets.token_urlsafe(32)
fd = os.open("usage-key.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as file:
    file.write(key)
print("sha256:", hashlib.sha256(key.encode()).hexdigest())
PYKEY
```

The digest covers the exact raw key bytes, with no added newline. Deliver the
private file to the pusher through your protected runtime; keep it out of Git.
Apply the config through the server's normal restart procedure.
The pusher sends `Authorization: Bearer $USAGE_KEY`; keep the key in its protected
environment, never in a sample committed to Git. The key can read usage and push
reports, imports and OTLP; it cannot access sessions, delete accounts or change
settings. Rotate by replacing the digest and updating the pusher; remove its config
entry to revoke. Ordinary authenticated user sessions also work. The old
`OMNIGENT_USAGE_READ_TOKEN` is deprecated; migrate it to this same configuration.

## Choose one route

### Subscription quota

Claude and Codex plans are read natively on connected hosts. Sign in on the host
and let its next read arrive. For an external quota reader, use account report v2.

### OpenTelemetry exporter

For a metered application, CLI or gateway that exports supported usage metrics
or spans, use OTLP/HTTP:

```sh
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_ENDPOINT="$OMNIGENT_URL/v1/me/usage/otlp"
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer $USAGE_KEY"
export OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=DELTA
```

The base appends `/v1/metrics`, `/v1/traces`, or `/v1/logs`. A signal-specific
endpoint must include the full suffix. JSON and protobuf work; logs are accepted
and discarded. Metrics must be delta (cumulative metrics are refused).
Supported mappings include Claude Code, Codex, Gemini CLI, LiteLLM and generic
GenAI usage. Unsupported metrics are ignored. Own-session samples are skipped
where the receiver can identify them; this is not universal duplicate detection.
OTLP is at-least-once: replay can count twice. Do not mix OTLP with report/import
activity on one account. Use separate account ids for independent writers.

### Official usage API

Send the vendor JSON unchanged to:

```text
POST /v1/me/usage/accounts/{id}/import?format={format}&observed_at={unix_seconds}
Content-Type: application/json
Authorization: Bearer $USAGE_KEY
```

| Format | Vendor response | Result |
| --- | --- | --- |
| `openai-usage` | Organization usage | Hourly tokens and requests |
| `openai-costs` | Organization costs | Daily billed cost |
| `anthropic-usage` | Organization usage report | Hourly token buckets |
| `anthropic-cost` | Organization cost report | Daily billed cost |
| `litellm-key` | `/key/info` | Key spend or budget window |
| `openrouter-key` | `/api/v1/key` | Key limits and spend |
| `openrouter-credits` | `/api/v1/credits` | Remaining USD credits |
| `newapi-user` | `/api/user/self` | Wallet, default 500000 units/USD |

OpenAI usage requests need `start_time`; request hourly usage buckets. Cost
imports require UTC daily buckets; usage imports require UTC hourly buckets.
An unaligned start, non-positive interval or bucket longer than its format's
grain is refused. A shorter aligned bucket is accepted as `partial`; even a
full bucket remains partial until its end is at least ten minutes before
`observed_at`. Import each page as read:
one response updates only its rows. Set `units_per_usd` for a new-api installation
whose conversion differs. Keep the source read time in `observed_at` when retrying.
Activity imports accept up to 400 days of history; wallet and quota readings
must be within one hour (at most five minutes ahead).

LiteLLM's key budget is not a wallet. With `max_budget: null`, only spend is
shown; no invented remaining amount. Its optional `budget_duration` and
`budget_reset_at` describe the budget period.

Run the maintained example with an ordinary LiteLLM app key, not a master key:

```sh
# Set these in your private runtime: LITELLM_URL, LITELLM_API_KEY,
# OMNIGENT_URL, USAGE_KEY, USAGE_ACCOUNT.
python scripts/examples/report_litellm_usage.py --check-only
python scripts/examples/report_litellm_usage.py
```

Run it with the Omnigent Python environment. The check uses the actual import
reader and prints only the validation result. Before pushing, the script keeps
only the documented budget fields inside `info`, excluding credentials and
provider configuration. It never logs or saves the raw response. Schedule it in your
own task runner if desired; stop that job to disconnect.

### Relay wallet

Use the official formats above when available. Balances have their own read
time and freshness and render as amount-only dashed rings. They never drive
pace, remaining quota, routing or spend totals. Different currencies and pools
stay separate; `usd_per_unit` is display metadata, not a spend conversion.

### Custom report and manually known balance

Use `PUT /v1/me/usage/accounts/{id}` with a v2 body. The Connect sheet gives a
complete copyable quota or balance example. A minimal balance report is:

```json
{"schema_version":2,"observed_at":1794000000,"balances":[{"id":"wallet","remaining":25,"unit":"USD"}]}
```

Replace `observed_at` with the current Unix time. Optional sections are `windows`,
`balances`, `activity`, `models` and display fields such as `display_name`,
`source_type` and `source`. Absent reading/model sections keep their values;
present sections, even empty, replace them. Activity replaces individual rows,
not the entire history. `windows` and `balances` refresh independently; activity
does not make an old quota current. Clear a balance with `balances: []`.
Limits per request: 16 windows, 16 balances, 500 activity entries, 128 models;
overflow returns 422 naming the section, with no truncation.

Use UTC hour starts for `activity[].hour`. Absent token buckets remain unknown,
not zero. A gap marker with no figures must say `coverage: partial`; partial
hours show **Incomplete coverage**, independently of any money estimate mark.
Complete rows resist lower or partial rereads; use `correction: true` only for
an intentional correction. `removed: true` removes a row by its existing key.

### Unofficial sources

An Antigravity/Cline proxy, web-login relay or unofficial Codex plan reader can
push account report v2 from a user-run script. These are examples, not built-in
scrapers: endpoints can change without notice and use may risk account suspension.
Prefer a supported exporter or official API whenever possible.

## Counting and write results

`billed` and `metered` cost count in money totals. `list` is a list-price estimate
(`estimate` remains a v1 alias); missing external cost is never silently priced
into spend. Keep estimates and bills in separate accounts. Pushed activity with a figure takes over native usage for the same host id
and UTC hour. Use the Omnigent host id, not its display name. Uncovered host-hours
keep their native rows. A pushed figure with no host id selects account-wide
takeover; a figure-less partial marker is not coverage evidence. Avoid sending
the same usage twice through separate accounts. Quota and balance readings use the newest
reading per account or pool, never a sum across hosts.

v2 and import writes return 200 with `applied`, `held`, `skipped` and
`older_sections`; inspect all four. v1 retains 204. An older quota or balance
section can be ignored while activity still applies. Serialize writes for one
account; resend an unknown-outcome write before sending the next. Moving rows
between accounts requires a removal and replacement; it is complete only when
neither response holds or skips an entry. There is no cross-account transaction.
See **Usage → Issues** for attributable rejected or failed source reports.

## State, changes and removal

The server database stores account readings, report outcomes and hourly activity
under your user. Find the account from its Usage tile; its gear opens settings,
including the source and last push time. Push another report to the same id to
update it. A failed read keeps the last values; an old read is marked old, an
absent window from a successful read disappears, a past-reset window is empty,
and never-read is not zero.

Display preferences (visibility, window pin, monthly fee, renewal, shown model)
are cached in this browser and synced to your user on the server in the
`usage_context` preference namespace. Find and change them in Settings or
Account settings. Reset a choice with its own control: select Auto for a window,
clear a monthly fee, custom renewal or shown model, or restore an account's
visibility switch. Those changes sync too. Clearing site data removes the local
cache; server-saved choices return on sign-in. They do not change vendor state.
Stop the pusher before **Account settings → Remove → Confirm remove**: removal
deletes the stored account reading and activity, while a subsequent push recreates
it. Native accounts are read from hosts and cannot be removed there.

Reference: [LiteLLM virtual keys](https://docs.litellm.ai/docs/proxy/virtual_keys),
[OTLP exporter configuration](https://opentelemetry.io/docs/specs/otel/protocol/exporter/).
