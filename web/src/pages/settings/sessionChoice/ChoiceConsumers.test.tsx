import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { usageBoardData } from "@/pages/usageBoard/usageBoardFixtures";
import { NewChatChoiceNotice } from "./NewChatChoiceNotice";
import { SessionChoiceCard, sessionChoiceRecord } from "./SessionChoiceCard";
import { QuotaFloorControl, floorActive } from "./QuotaFloorControl";
import { ProjectSessionChoiceField } from "./ProjectSessionChoiceField";

const mocks = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("./sessionChoiceData", () => ({
  choiceRequest: mocks.request,
  useSessionChoiceDocument: () => ({
    data: { settings: { enabled: true, presets: [{ id: "code", name: "Code" }] } },
  }),
}));
beforeEach(() => {
  mocks.request.mockReset();
});
afterEach(cleanup);
function mount(ui: React.ReactNode) {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      {ui}
    </QueryClientProvider>,
  );
}
it("renders a chunked choice record in a designed card and ignores broken labels", () => {
  const record = {
    agent: "a",
    work_type: "review",
    tier: 2,
    model: "model-a",
    meter: "plan",
    reason: "First eligible tier",
    skipped: [{ link: { agent: "b" }, why: "busy", running: ["session-b"] }],
  };
  const text = JSON.stringify(record);
  const labels = {
    "omnigent.session_choice": '{"chunks":2}',
    "omnigent.session_choice.0": text.slice(0, 25),
    "omnigent.session_choice.1": text.slice(25),
  };
  expect(sessionChoiceRecord({ "omnigent.session_choice": "broken" })).toBeNull();
  expect(sessionChoiceRecord({ "omnigent.session_choice": '{"chunks":300}' })).toBeNull();
  for (const patch of [{ skipped: [{}] }, { skipped: "broken" }, { model: {} }, { flags: [2] }]) {
    expect(
      sessionChoiceRecord({ "omnigent.session_choice": JSON.stringify({ ...record, ...patch }) }),
    ).toBeNull();
  }
  mount(<SessionChoiceCard labels={labels} agentName="Coding agent" />);
  fireEvent.click(screen.getByRole("button", { name: "Why this agent was chosen" }));
  expect(screen.getByText("First eligible tier")).toBeInTheDocument();
  expect(screen.getByText("Running: session-b")).toBeInTheDocument();
  expect(screen.getByText("model-a")).toBeInTheDocument();
});

it("shows the live queued marker on a held choice", () => {
  mount(
    <SessionChoiceCard
      labels={{
        "omnigent.session_choice": JSON.stringify({
          agent: "a",
          work_type: "work",
          reason: "Waiting for a slot",
        }),
        "omnigent.startup.session_choice": "queued@host-a@plan",
      }}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Why this agent was chosen" }));
  expect(
    screen.getByText("Queued. Waiting for capacity or reserve to become available."),
  ).toBeInTheDocument();
});
it("shows no notice while a background request is pending or fails", async () => {
  let reject!: (error: Error) => void;
  mocks.request.mockImplementation(
    () =>
      new Promise((_, fail) => {
        reject = fail;
      }),
  );
  mount(
    <>
      <button type="button">Start session</button>
      <NewChatChoiceNotice host="host-a" agent="agent-a" model="model-a" />
    </>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Start session" }));
  expect(screen.queryByRole("note")).not.toBeInTheDocument();
  await waitFor(() => expect(mocks.request).toHaveBeenCalled());
  reject(new Error("offline"));
  await waitFor(() => expect(screen.queryByRole("note")).not.toBeInTheDocument());
});
it("reads only the picked model's notice", async () => {
  mocks.request.mockResolvedValue({ notice: "Coding plan 7d: 4% left" });
  mount(<NewChatChoiceNotice host="host a" agent="agent-a" model="provider/model" />);
  expect(await screen.findByRole("note")).toHaveTextContent("4% left");
  expect(mocks.request.mock.calls[0][0]).toContain("model=provider%2Fmodel");
});
it("keeps a floor override until the first observed reset and manual use up indefinitely", () => {
  const now = Date.now() / 1000;
  const entry = {
    until: "reset" as const,
    windows: { "7d": [now + 86400, 80] as [number, number] },
  };
  const reading = {
    ...usageBoardData().accounts[0],
    windows: [
      { ...usageBoardData().accounts[0].windows[1], resets_at: now + 86400, percent_used: 76 },
    ],
  };
  expect(floorActive(entry, reading)).toBe(true);
  expect(
    floorActive(entry, { ...reading, windows: [{ ...reading.windows[0], percent_used: 75 }] }),
  ).toBe(false);
  expect(floorActive(entry, { ...reading, windows: [] })).toBe(false);
  expect(floorActive({ until: "manual" }, undefined)).toBe(true);
});
it("writes the tile floor immediately and reports a failure without pretending it saved", async () => {
  mocks.request.mockImplementation((_, init) => (init ? Promise.resolve({}) : Promise.resolve({})));
  mount(<QuotaFloorControl tile="plan" label="Coding plan" />);
  const control = await screen.findByLabelText("Coding plan floor");
  fireEvent.change(control, { target: { value: "manual" } });
  await waitFor(() =>
    expect(mocks.request).toHaveBeenCalledWith(
      "/floor",
      expect.objectContaining({ body: JSON.stringify({ tile: "plan", off: true, keep: true }) }),
    ),
  );
  await waitFor(() => expect(control).not.toBeDisabled());
  mocks.request.mockRejectedValueOnce(new Error("No reset reading"));
  fireEvent.change(control, { target: { value: "reset" } });
  expect(await screen.findByRole("alert")).toHaveTextContent("No reset reading");
});
it("offers account inheritance, off and a preset pin for a project", () => {
  const change = vi.fn();
  mount(<ProjectSessionChoiceField value={{ pin: "deleted" }} onChange={change} />);
  const field = screen.getByLabelText("Project session choice");
  expect(field).toHaveValue("pin:deleted");
  fireEvent.change(field, { target: { value: "pin:code" } });
  expect(change).toHaveBeenLastCalledWith({ pin: "code" });
  fireEvent.change(field, { target: { value: "off" } });
  expect(change).toHaveBeenLastCalledWith("off");
  fireEvent.change(field, { target: { value: "inherit" } });
  expect(change).toHaveBeenLastCalledWith(undefined);
});
