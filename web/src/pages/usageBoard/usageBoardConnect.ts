// The sample a source copies out of Connect. It has to be a command that
// actually submits: the shell substitutes a numeric observation time, and the
// sample window resets two weeks from the moment it runs, so the report passes
// the route's fields instead of being rejected as a string or a past reset.

/** One sample window, two weeks out — long enough that the copied text stays valid. */
export const CONNECT_SAMPLE_RESET_SECONDS = 14 * 86_400;

/** `$(date +%s)`, or that plus an offset, in seconds since the epoch. */
const NOW_SECONDS = "$(date +%s)";

function secondsFromNow(offset: number): string {
  return offset === 0 ? NOW_SECONDS : `$(( ${NOW_SECONDS} + ${offset} ))`;
}

/**
 * The command Connect shows. The JSON is written first and its quotes escaped
 * for the shell's double-quoted string, so the arithmetic substitution lands as
 * a bare number: a single-quoted body would leave `observed_at` a string and the
 * route rejects that with 422.
 */
export function buildConnectCommand(origin: string, source: ConnectSource = "report"): string {
  const window =
    `{"id":"month","percent_used":41,"window_minutes":43200,` +
    `"resets_at":${secondsFromNow(CONNECT_SAMPLE_RESET_SECONDS)}}`;
  let json = `{"schema_version":2,"observed_at":${secondsFromNow(0)},"windows":[${window}]}`;
  let endpoint = `${origin}/v1/me/usage/accounts/my-gateway`;
  let method = "PUT";
  if (source === "manual" || source === "undocumented") {
    json = `{"schema_version":2,"observed_at":${secondsFromNow(0)},"source_type":"${source === "manual" ? "manual" : "other"}","balances":[{"id":"wallet","remaining":25,"unit":"USD"}]}`;
  } else if (source === "vendor" || source === "relay") {
    method = "POST";
    endpoint += `/import?format=${source === "vendor" ? "litellm-key" : "openrouter-credits"}`;
    json =
      source === "vendor"
        ? '{"info":{"spend":12,"max_budget":null}}'
        : '{"data":{"total_credits":50,"total_usage":12}}';
  } else if (source === "otel") {
    method = "POST";
    endpoint = `${origin}/v1/me/usage/otlp/v1/metrics`;
    json = `{"resourceMetrics":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"my-app"}}]},"scopeMetrics":[{"metrics":[{"name":"gen_ai.client.inference.usage.input_tokens","sum":{"aggregationTemporality":1,"dataPoints":[{"timeUnixNano":"${secondsFromNow(0)}000000000","asInt":"100"}]}}]}]}]}`;
  }
  const quoted = json.replace(/"/g, '\\"');
  return [
    `curl --fail-with-body -X ${method} "${endpoint}" \\`,
    '  -H "Authorization: Bearer $USAGE_KEY" \\',
    '  -H "Content-Type: application/json" \\',
    `  -d "${quoted}"`,
  ].join("\n");
}

export const CONNECT_SOURCES = [
  "subscription",
  "otel",
  "vendor",
  "relay",
  "report",
  "manual",
  "undocumented",
] as const;
export type ConnectSource = (typeof CONNECT_SOURCES)[number];

/** The command with a shell's arithmetic substitutions applied, for tests. */
export function expandConnectCommand(command: string, nowSeconds: number): string {
  return command
    .replace(/\$\(\(\s*\$\(date \+%s\)\s*\+\s*(\d+)\s*\)\)/g, (_match, offset: string) =>
      String(nowSeconds + Number(offset)),
    )
    .replace(/\$\(date \+%s\)/g, String(nowSeconds));
}

/** The JSON body a shell would hand to `curl -d`, parsed. */
export function connectCommandPayload(command: string, nowSeconds: number): unknown {
  const expanded = expandConnectCommand(command, nowSeconds);
  const body = /-d "([\s\S]*)"\s*$/.exec(expanded);
  if (!body) throw new Error("The command carries no -d body");
  return JSON.parse(body[1].replace(/\\"/g, '"'));
}
