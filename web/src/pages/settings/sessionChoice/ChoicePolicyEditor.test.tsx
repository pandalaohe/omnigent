import { useState } from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
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
