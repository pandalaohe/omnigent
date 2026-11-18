import { useState } from "react";
import { cleanup, fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { Host } from "@/hooks/useHosts";
import { ChoicePolicyEditor } from "./ChoicePolicyEditor";
import { ChoiceChainEditor } from "./ChoiceLinkEditor";
import { usageBoardData } from "@/pages/usageBoard/usageBoardFixtures";
import { EMPTY_CHOICE, type ChoiceLink, type SessionChoiceDocument } from "./sessionChoiceData";

vi.mock("@/hooks/useHosts", () => ({
  useHostModelOptions: () => ({
    data: [{ id: "model-a", displayName: "Model A", efforts: ["high"] }],
  }),
}));
afterEach(cleanup);
const host = {
  host_id: "a",
  name: "Desktop",
  configured_harnesses: { codex: true },
} as unknown as Host;
const agents = [
  { id: "agent", name: "agent", display_name: "Coding agent", harness: "codex", product: "codex" },
];
function document(): SessionChoiceDocument {
  return {
    ...structuredClone(EMPTY_CHOICE),
    workTypes: [
      { id: "work", label: "Work" },
      { id: "review", label: "Review" },
    ],
    presets: [{ id: "code", name: "Code", links: [{ agent: "agent", model: "model-a" }] }],
    hosts: {
      a: {
        work: [
          [
            { preset: "code", links: [{ agent: "agent" }] },
            { preset: "code", adjusted: true, links: [{ agent: "agent", model: "custom" }] },
          ],
        ],
        review: [[]],
      },
    },
  };
}
function mount(initial = document()) {
  function App() {
    const [value, setValue] = useState(initial);
    return (
      <>
        <ChoicePolicyEditor
          document={value}
          host="a"
          hosts={[host]}
          agents={agents}
          meters={[]}
          onChange={setValue}
        />
        <output data-testid="document">{JSON.stringify(value)}</output>
      </>
    );
  }
  return render(<App />);
}
const value = () =>
  JSON.parse(screen.getByTestId("document").textContent!) as SessionChoiceDocument;

it("edits fallback modes in a preset, defaults to Return and omits Return from the draft", () => {
  const initial = document();
  initial.presets[0].links = [
    { agent: "agent", model: "head", fallbackMode: "drain" },
    { agent: "agent", model: "backup" },
    { agent: "agent", model: "legacy-null", fallbackMode: null },
    { agent: "agent", model: "explicit-return", fallbackMode: "return" },
  ];
  mount(initial);
  fireEvent.click(screen.getByRole("button", { name: "Code" }));
  const groups = screen.getAllByRole("radiogroup");
  expect(groups).toHaveLength(3);
  for (const group of groups) {
    expect(within(group).getByRole("radio", { name: "Return" })).toBeChecked();
    expect(group).toHaveAccessibleDescription("Go back as soon as an earlier node recovers");
  }
  const fallback = within(screen.getByRole("radiogroup", { name: "Fallback 1" }));
  fireEvent.click(fallback.getByRole("radio", { name: "Drain" }));
  expect(fallback.getByRole("radio", { name: "Drain" })).toBeChecked();
  expect(groups[0]).toHaveAccessibleDescription("Use this node up before going back");
  expect(value().presets[0].links[1].fallbackMode).toBe("drain");
  fireEvent.click(fallback.getByRole("radio", { name: "Return" }));
  expect(value().presets[0].links[1]).not.toHaveProperty("fallbackMode");
  expect(value().presets[0].links[0].fallbackMode).toBe("drain");
});

it("edits links in place, protects adjusted rows from Apply, and restores a row", () => {
  mount();
  fireEvent.change(screen.getAllByRole("spinbutton")[0], { target: { value: "5" } });
  expect(value().hosts.a.work[0][0]).toMatchObject({ weight: 5 });
  expect(value().hosts.a.work[0][0].adjusted).toBeUndefined();
  fireEvent.click(screen.getByRole("button", { name: "Code" }));
  fireEvent.change(screen.getByLabelText("Preset name"), { target: { value: "New name" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply to 1 rows" }));
  expect(screen.getByText("1 updated, 1 adjusted left as they are.")).toBeInTheDocument();
  expect(value().hosts.a.work[0][0]).toMatchObject({ name: "New name", weight: 5 });
  expect(value().hosts.a.work[0][1].links[0].model).toBe("custom");
  const tier = screen.getByLabelText("Tier 1");
  fireEvent.click(within(tier).getByRole("button", { name: /New name.*Coding agent/ }));
  fireEvent.change(within(tier).getByLabelText("Link model"), { target: { value: "typed-model" } });
  expect(value().hosts.a.work[0][0].adjusted).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Back to preset" }));
  expect(value().hosts.a.work[0][0]).toMatchObject({
    adjusted: false,
    links: [{ model: "model-a" }],
  });
});

it("copies a work type to another and Undo restores all previous rows", () => {
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Review" }));
  fireEvent.change(screen.getByLabelText("Copy from work type"), { target: { value: "work" } });
  fireEvent.click(screen.getByRole("button", { name: "Copy" }));
  expect(value().hosts.a.review).toEqual(value().hosts.a.work);
  fireEvent.change(screen.getByLabelText("Work type definition"), {
    target: { value: "Retain this edit" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  expect(value().hosts.a.review).toEqual([[]]);
  expect(value().workTypes[1].definition).toBe("Retain this edit");
});

it("renames work types, guards default deletion, and adds a fallback", () => {
  mount();
  fireEvent.click(screen.getByRole("button", { name: "Delete work type" }));
  expect(screen.getByRole("alert")).toHaveTextContent("default work type");
  fireEvent.change(screen.getByLabelText("Work type name"), { target: { value: "Big design" } });
  fireEvent.blur(screen.getByLabelText("Work type name"));
  expect(value().workTypes[0]).toMatchObject({ id: "big-design", aliases: ["work"] });
  fireEvent.click(screen.getByRole("button", { name: "Code" }));
  fireEvent.click(screen.getByRole("button", { name: "+ Fallback" }));
  fireEvent.change(screen.getAllByLabelText("Link agent")[1], { target: { value: "agent" } });
  expect(value().presets[0].links[1]).toMatchObject({ agent: "agent" });
  fireEvent.click(screen.getAllByRole("button", { name: "Remove link" })[1]);
  expect(value().presets[0].links).toHaveLength(1);
});

it("offers generic starters and keyboard drag handles without move buttons", () => {
  mount(structuredClone(EMPTY_CHOICE));
  fireEvent.click(screen.getByRole("button", { name: "Simple" }));
  expect(screen.getByRole("spinbutton", { name: "Coding agent weight in Tier 1" })).toHaveValue(1);
  expect(screen.getByRole("button", { name: "Drag Coding agent in Tier 1" })).toHaveAttribute(
    "tabindex",
    "0",
  );
  expect(screen.queryByRole("button", { name: /move up|move down/i })).not.toBeInTheDocument();
});

it("guesses a meter as identity changes until the user chooses it by hand", () => {
  function Chain() {
    const [links, setLinks] = useState<ChoiceLink[]>([{ agent: "" }]);
    return (
      <ChoiceChainEditor
        host={host}
        agents={agents}
        meters={usageBoardData().accounts}
        links={links}
        onChange={setLinks}
      />
    );
  }
  render(<Chain />);
  fireEvent.change(screen.getByLabelText("Link agent"), { target: { value: "agent" } });
  expect(screen.getByLabelText("Link meter")).toHaveValue("codex");
  fireEvent.change(screen.getByLabelText("Link model"), { target: { value: "provider/model" } });
  expect(screen.getByLabelText("Link meter")).toHaveValue("");
  fireEvent.change(screen.getByLabelText("Link meter"), { target: { value: "codex" } });
  fireEvent.change(screen.getByLabelText("Link model"), { target: { value: "another/model" } });
  expect(screen.getByLabelText("Link meter")).toHaveValue("codex");
});

it("moves work types with keyboard insertion feedback and preserves the selection", async () => {
  mount();
  const handle = screen.getByRole("button", { name: "Drag Work work type" });
  handle.focus();
  fireEvent.keyDown(handle, { key: " " });
  fireEvent.keyDown(handle, { key: "ArrowDown" });
  expect(screen.getByRole("status", { name: "Drag result" })).toHaveTextContent(
    "Move Work work type after Review work type",
  );
  fireEvent.keyDown(handle, { key: "Enter" });
  expect(value().workTypes.map((work) => work.id)).toEqual(["review", "work"]);
  expect(screen.getByRole("button", { name: "Work" })).toHaveAttribute("aria-current", "true");
  await waitFor(() => expect(handle).toHaveFocus());
  expect(
    screen.getByText(/Move Work work type after Review work type. Not saved yet/),
  ).toBeInTheDocument();
});

it("moves a row into an empty tier, keeps its data and restores focus", async () => {
  const initial = document();
  initial.hosts.a.work.push([]);
  mount(initial);
  const first = screen.getAllByRole("button", { name: "Drag Code in Tier 1" })[0];
  first.focus();
  fireEvent.keyDown(first, { key: " " });
  fireEvent.keyDown(first, { key: "ArrowRight" });
  expect(screen.getByRole("status", { name: "Drag result" })).toHaveTextContent(
    "Move Code in Tier 1 into Tier 2 as the first entry",
  );
  fireEvent.keyDown(first, { key: "Enter" });
  expect(value().hosts.a.work[1]).toEqual([initial.hosts.a.work[0][0]]);
  expect(value().hosts.a.work[0][0].adjusted).toBe(true);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Drag Code in Tier 2" })).toHaveFocus(),
  );
});

it("copies presets and moves ability entries between levels without removing the preset", async () => {
  const initial = document();
  initial.abilityOrder = [[], []];
  mount(initial);
  fireEvent.click(screen.getByText("Ability order · Delegation ceiling"));
  const preset = screen.getByRole("button", { name: "Drag Code" });
  fireEvent.keyDown(preset, { key: " " });
  fireEvent.keyDown(preset, { key: "ArrowRight" });
  fireEvent.keyDown(preset, { key: "Enter" });
  expect(value().abilityOrder).toEqual([["code"], []]);
  expect(value().presets).toEqual(initial.presets);
  const ability = screen.getByRole("button", { name: "Drag code ability" });
  fireEvent.keyDown(ability, { key: " " });
  fireEvent.keyDown(ability, { key: "ArrowRight" });
  fireEvent.keyDown(ability, { key: "Enter" });
  expect(value().abilityOrder).toEqual([[], ["code"]]);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Drag code ability" })).toHaveFocus(),
  );
});

it("Escape cancels a proposed move and leaves all draft data unchanged", async () => {
  const initial = document();
  mount(initial);
  const handle = screen.getByRole("button", { name: "Drag Work work type" });
  handle.focus();
  fireEvent.keyDown(handle, { key: " " });
  fireEvent.keyDown(handle, { key: "End" });
  fireEvent.keyDown(handle, { key: "Escape" });
  expect(value()).toEqual(initial);
  expect(screen.queryAllByText("Move Work work type after Review work type")).toHaveLength(0);
  expect(screen.getByText("Move cancelled.")).toBeInTheDocument();
  await waitFor(() => expect(handle).toHaveFocus());
});

it("explains the ability ceiling through the existing focusable help control", () => {
  mount();
  fireEvent.click(screen.getByText("Ability order · Delegation ceiling"));
  fireEvent.focus(screen.getByRole("button", { name: "About Ability order" }));
  expect(screen.getByText(/Codex at Level 2 cannot delegate to Claude at Level 1/)).toBeVisible();
  expect(screen.getByText(/If either side has no level mapping/)).toBeVisible();
});

it("copies a preset to the first tier position and focuses its new copy", async () => {
  const initial = document();
  mount(initial);
  const preset = screen.getByRole("button", { name: "Drag Code" });
  fireEvent.keyDown(preset, { key: " " });
  fireEvent.keyDown(preset, { key: "Home" });
  expect(screen.getByRole("status", { name: "Drag result" })).toHaveTextContent(
    "Copy Code before Code in Tier 1",
  );
  fireEvent.keyDown(preset, { key: "Enter" });
  expect(value().hosts.a.work[0]).toHaveLength(3);
  expect(value().hosts.a.work[0][0]).toEqual({
    preset: "code",
    name: "Code",
    links: initial.presets[0].links,
  });
  expect(value().presets).toEqual(initial.presets);
  await waitFor(() =>
    expect(screen.getAllByRole("button", { name: "Drag Code in Tier 1" })[0]).toHaveFocus(),
  );
});

it("cancels a proposed row move when switching work types", () => {
  const initial = document();
  mount(initial);
  const handle = screen.getAllByRole("button", { name: "Drag Code in Tier 1" })[0];
  fireEvent.keyDown(handle, { key: " " });
  fireEvent.keyDown(handle, { key: "End" });
  fireEvent.click(screen.getByRole("button", { name: "Review" }));
  expect(value()).toEqual(initial);
  expect(screen.getByRole("status", { name: "Drag result" })).toHaveTextContent(
    "Move cancelled: policy changed.",
  );
});

it("keeps Weight and Link model inputs focused across consecutive edits", () => {
  mount();
  const weight = screen.getAllByRole("spinbutton")[0];
  weight.focus();
  fireEvent.change(weight, { target: { value: "4" } });
  expect(weight).toHaveFocus();
  fireEvent.change(weight, { target: { value: "5" } });
  expect(value().hosts.a.work[0][0].weight).toBe(5);
  const tier = screen.getByLabelText("Tier 1");
  fireEvent.click(within(tier).getAllByRole("button", { name: /Code.*Coding agent/ })[0]);
  const model = within(tier).getByLabelText("Link model");
  model.focus();
  fireEvent.change(model, { target: { value: "model-x" } });
  expect(model).toHaveFocus();
  fireEvent.change(model, { target: { value: "model-xy" } });
  expect(value().hosts.a.work[0][0].links[0].model).toBe("model-xy");
});

it("Tab away and Escape on a normal control cannot leave a stale drag to commit", () => {
  const initial = document();
  mount(initial);
  const handle = screen.getByRole("button", { name: "Drag Work work type" });
  fireEvent.keyDown(handle, { key: " " });
  fireEvent.keyDown(handle, { key: "End" });
  fireEvent.keyDown(handle, { key: "Tab" });
  const normal = screen.getByRole("button", { name: "Work" });
  normal.focus();
  fireEvent.keyDown(normal, { key: "Escape" });
  expect(screen.queryAllByText("Move Work work type after Review work type")).toHaveLength(0);
  fireEvent.keyDown(screen.getByRole("button", { name: "Drag Review work type" }), {
    key: "Enter",
  });
  expect(value()).toEqual(initial);
});

it("offers only ready host agents, disambiguates duplicate labels and shows meter-owned caps", () => {
  const available = [
    { ...agents[0], id: "pi-cli", name: "pi-cli", display_name: "Pi", harness: "pi-native" },
    { ...agents[0], id: "pi-rpc", name: "pi-rpc", display_name: "Pi", harness: "pi" },
    { ...agents[0], id: "login", name: "login", display_name: "Needs login", harness: "login" },
    { ...agents[0], id: "missing", name: "missing", display_name: "Missing", harness: "missing" },
    { ...agents[0], id: "old", name: "old", display_name: "Old", harness: "old" },
  ];
  function App() {
    const [links, setLinks] = useState<ChoiceLink[]>([{ agent: "pi-cli", maxRunning: 2 }]);
    return (
      <ChoiceChainEditor
        links={links}
        onChange={setLinks}
        agents={available}
        host={{
          ...host,
          configured_harnesses: {
            "pi-native": true,
            pi: true,
            login: "needs-auth",
            missing: "binary-missing",
            old: "version-too-low",
          },
        }}
        meters={usageBoardData().accounts}
        limits={{ codex: { maxRunning: 3 } }}
      />
    );
  }
  render(<App />);
  const picker = screen.getByRole("combobox", { name: "Link agent" });
  expect(
    within(picker)
      .getAllByRole("option")
      .map((item) => item.textContent),
  ).toEqual(["Choose an agent", "Pi · pi-native · pi-cli", "Pi · pi · pi-rpc"]);
  expect(screen.getByRole("spinbutton", { name: "Link max running" })).toHaveValue(2);
  fireEvent.change(screen.getByRole("combobox", { name: "Link meter" }), {
    target: { value: "codex" },
  });
  expect(screen.queryByRole("spinbutton", { name: "Link max running" })).not.toBeInTheDocument();
  expect(screen.getByRole("note")).toHaveTextContent("Meter max running: 3");
  expect(screen.getByRole("link", { name: "Edit Limits" })).toHaveAttribute(
    "href",
    "#session-choice-limits",
  );
  expect(screen.getByText(/The quota counter this link spends from/)).toBeInTheDocument();
  fireEvent.change(screen.getByRole("combobox", { name: "Link meter" }), { target: { value: "" } });
  expect(screen.getByRole("spinbutton", { name: "Link max running" })).toHaveValue(null);
});

it("opens the shared preset from an ability entry and explains its other uses", async () => {
  const initial = document();
  initial.abilityOrder = [["code"]];
  mount(initial);
  fireEvent.click(screen.getByText("Ability order · Delegation ceiling"));
  fireEvent.click(screen.getByRole("button", { name: "Edit Code preset from ability order" }));
  expect(screen.getByLabelText("Preset name")).toHaveValue("Code");
  expect(screen.getByRole("note")).toHaveTextContent("used in 2 other places");
  expect(screen.getByRole("note")).toHaveTextContent("adjusted rows keep their own links");
  await waitFor(() => expect(screen.getByLabelText("Preset name")).toHaveFocus());
  fireEvent.change(screen.getByLabelText("Preset name"), { target: { value: "Renamed code" } });
  expect(value().presets[0].name).toBe("Renamed code");
  expect(
    screen.getByRole("button", { name: "Edit Renamed code preset from ability order" }),
  ).toBeInTheDocument();
});
