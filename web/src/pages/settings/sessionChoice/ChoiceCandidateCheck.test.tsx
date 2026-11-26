import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { Host } from "@/hooks/useHosts";
import { ChoiceCandidateCheck } from "./ChoiceCandidateCheck";
import { EMPTY_CHOICE, type ChoiceCandidatePreview } from "./sessionChoiceData";
import type * as ChoiceData from "./sessionChoiceData";

const request = vi.hoisted(() => vi.fn());
vi.mock("./sessionChoiceData", async (original) => ({
  ...(await original<typeof ChoiceData>()),
  choiceRequest: request,
}));
afterEach(() => {
  cleanup();
  request.mockReset();
});
const document = {
  ...structuredClone(EMPTY_CHOICE),
  schemaVersion: 2,
  limitRules: [{ id: "base", agent: "agent-a", maxRunning: 3 }],
};
const props = {
  document,
  host: "host-a",
  revision: "saved-a",
  hosts: [{ host_id: "host-a", name: "Desktop" }] as Host[],
  agents: [{ id: "agent-a", name: "agent-a", display_name: "Agent A", harness: "codex" }],
};
function response(): ChoiceCandidatePreview {
  return {
    mode: "candidate",
    candidate: { agent_id: "agent-a", host_id: "host-a", model: "model-x" },
    policyActive: false,
    configuredInTiers: false,
    admission: false,
    savedRevision: "saved-a",
    draftFingerprint: "draft-a",
    effectiveLimits: {
      maxRunning: {
        defined: true,
        value: 3,
        sourceRuleId: "base",
        specificity: 1,
        scope: { agent: "agent-a" },
        countState: "unknown",
        runningCount: null,
        reservedCount: null,
      },
    },
    legacyChecks: { pool: "pool-a", limits: { maxRunning: 2 }, linkMaxRunning: null },
    lines: [
      { state: "unknown", warn_line_pct: null, stop_line_pct: null, released: false },
      { state: "warn", warn_line_pct: 12, stop_line_pct: 2, released: true },
    ],
    check: { state: "unknown", reason: "count_unavailable" },
  };
}
function open() {
  fireEvent.click(screen.getByText("Check a combination"));
  fireEvent.change(screen.getByRole("combobox", { name: "Preview agent" }), {
    target: { value: "agent-a" },
  });
}

it("checks the exact draft outside tiers and distinguishes unknown counts, legacy and inactive policy", async () => {
  request.mockResolvedValue(response());
  render(<ChoiceCandidateCheck {...props} />);
  open();
  fireEvent.click(screen.getByRole("button", { name: "Check draft" }));
  await screen.findByText(/count_unavailable/);
  const body = JSON.parse(request.mock.calls[0][1].body);
  expect(body).toEqual({
    mode: "candidate",
    host_id: "host-a",
    candidate: { agent_id: "agent-a", model: null },
    settings: document,
  });
  expect(screen.getByText(/Outside tiers/)).toHaveTextContent("does not reserve a session");
  expect(screen.getByText(/Running:/)).toHaveTextContent("Unknown");
  expect(screen.getByText(/Legacy pool constraints/)).toHaveTextContent("pool-a");
  expect(screen.getByText(/Source: rule 1/)).toHaveTextContent("All hosts / All models");
  expect(screen.getByText(/Quota check:/)).toHaveTextContent(
    "Quota check: unknown · Warning: Unknown / unset",
  );
  expect(screen.getByText(/Quota check:/)).toHaveTextContent(
    "Quota check: warn · Warning: 12% · Stop: 2% · Floor released",
  );
});

it("uses typeahead when the agent and host catalogs exceed five options", async () => {
  request.mockResolvedValue(response());
  const agents = Array.from({ length: 6 }, (_, index) => ({
    id: `agent-${index}`,
    name: `agent-${index}`,
    display_name: `Agent ${index}`,
    harness: "codex",
  }));
  const hosts = Array.from({ length: 6 }, (_, index) => ({
    host_id: `host-${index}`,
    name: `Host ${index}`,
    status: "online" as const,
  })) as Host[];
  render(<ChoiceCandidateCheck {...props} host="host-0" hosts={hosts} agents={agents} />);
  fireEvent.click(screen.getByText("Check a combination"));
  const agent = screen.getByRole("combobox", { name: "Preview agent" });
  fireEvent.change(agent, { target: { value: "Agent 5" } });
  fireEvent.click(screen.getByRole("option", { name: /Agent 5.*agent-5/ }));
  fireEvent.click(screen.getByRole("button", { name: "Check draft" }));
  await waitFor(() => expect(request).toHaveBeenCalledOnce());
  expect(JSON.parse(request.mock.calls[0][1].body).candidate.agent_id).toBe("agent-5");
});

it("discards an in-flight explanation after the draft changes", async () => {
  let resolve!: (data: ChoiceCandidatePreview) => void;
  request.mockImplementation(
    () =>
      new Promise<ChoiceCandidatePreview>((done) => {
        resolve = done;
      }),
  );
  const view = render(<ChoiceCandidateCheck {...props} />);
  open();
  fireEvent.click(screen.getByRole("button", { name: "Check draft" }));
  await waitFor(() => expect(request).toHaveBeenCalledOnce());
  view.rerender(<ChoiceCandidateCheck {...props} document={{ ...document, limitRules: [] }} />);
  await act(async () => {
    resolve(response());
  });
  expect(screen.queryByText(/count_unavailable/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Check draft" })).not.toBeDisabled();
});

it("shows unresolved candidate errors without presenting a successful explanation", async () => {
  request.mockRejectedValue(new Error("candidate_unresolved"));
  render(<ChoiceCandidateCheck {...props} />);
  open();
  fireEvent.click(screen.getByRole("button", { name: "Check draft" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("candidate_unresolved");
  expect(screen.queryByText(/Passes current checks/)).not.toBeInTheDocument();
});

it("renders multi-member scopes and every binding cap rule with its running count", async () => {
  const multi = response();
  multi.check = { state: "blocked", reason: "busy" };
  multi.effectiveLimits.maxRunning = {
    defined: true,
    value: 3,
    sourceRuleId: "opus-cap",
    specificity: null,
    scope: null,
    members: [
      { agent: "agent-a", host: "host-a", model: "opus-5" },
      { agent: "agent-b", host: "host-b", model: "opus-5" },
    ],
    countState: "known",
    runningCount: 3,
    reservedCount: 1,
    bindingRules: [
      {
        ruleId: "opus-cap",
        maxRunning: 3,
        members: [
          { agent: "agent-a", host: "host-a", model: "opus-5" },
          { agent: "agent-b", host: "host-b", model: "opus-5" },
        ],
        runningCount: 3,
        reservedCount: 1,
        countState: "known",
      },
      {
        ruleId: "base",
        maxRunning: 5,
        members: [{ agent: "agent-a" }],
        runningCount: 2,
        reservedCount: 0,
        countState: "known",
      },
    ],
  };
  request.mockResolvedValue(multi);
  render(
    <ChoiceCandidateCheck
      {...props}
      document={{
        ...document,
        limitRules: [
          { id: "base", agent: "agent-a", maxRunning: 5 },
          {
            id: "opus-cap",
            members: [
              { agent: "agent-a", host: "host-a", model: "opus-5" },
              { agent: "agent-b", host: "host-b", model: "opus-5" },
            ],
            maxRunning: 3,
          },
        ],
      }}
    />,
  );
  open();
  fireEvent.click(screen.getByRole("button", { name: "Check draft" }));
  await screen.findByText(/Binding caps:/);
  expect(screen.getByText(/opus-cap: 3\/3/)).toBeInTheDocument();
  expect(screen.getByText(/base: 2\/5/)).toBeInTheDocument();
});
