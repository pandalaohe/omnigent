import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Host } from "@/hooks/useHosts";
import { ChoiceLinkEditor } from "./ChoiceLinkEditor";
import type { ChoiceLink } from "./sessionChoiceData";

const catalog = vi.hoisted(() => ({ data: [] as { id: string }[] }));
vi.mock("@/hooks/useHosts", () => ({ useHostModelOptions: () => catalog }));
afterEach(cleanup);
beforeEach(() => {
  catalog.data = [{ id: "provider-a/x" }];
});

function Editor({ initial }: { initial?: string }) {
  const [link, setLink] = useState<ChoiceLink>({ agent: "agent-a", model: initial });
  return (
    <>
      <ChoiceLinkEditor
        link={link}
        agents={[
          {
            id: "agent-a",
            name: "agent-a",
            display_name: "Agent A",
            harness: "codex",
            product: "codex",
          },
        ]}
        host={{ host_id: "host-a", configured_harnesses: { codex: true } } as unknown as Host}
        meters={[]}
        onChange={setLink}
        onRemove={() => {}}
        removable={false}
        meterPicked
        onPickMeter={() => {}}
      />
      <output aria-label="Saved model">{link.model}</output>
      <output aria-label="Saved permission tier">{link.permissionTier ?? "none"}</output>
    </>
  );
}

it("defaults the sole provider and saves the full id", () => {
  render(<Editor />);
  fireEvent.change(screen.getByLabelText("Link model"), { target: { value: "x" } });
  expect(screen.getByLabelText("Saved model")).toHaveTextContent("provider-a/x");
  expect(screen.getByLabelText("Link model")).toHaveValue("x");
  expect(screen.queryByLabelText("Link provider")).not.toBeInTheDocument();
});

it("shows a provider choice for competing offers of the same model", () => {
  catalog.data = [{ id: "provider-a/x" }, { id: "provider-b/x" }];
  render(<Editor initial="provider-a/x" />);
  fireEvent.change(screen.getByLabelText("Link provider"), { target: { value: "provider-b/x" } });
  expect(screen.getByLabelText("Saved model")).toHaveTextContent("provider-b/x");
  expect(screen.getByLabelText("Link model")).toHaveValue("x");
});

it("keeps an absent saved provider visible and preserves full typed ids", () => {
  catalog.data = [{ id: "provider-a/x" }, { id: "provider-b/x" }];
  render(<Editor initial="old/x" />);
  expect(screen.getByRole("option", { name: "old (not offered here)" })).toBeInTheDocument();
  expect(screen.getByLabelText("Saved model")).toHaveTextContent("old/x");
  fireEvent.change(screen.getByLabelText("Link model"), { target: { value: "provider-b/x" } });
  expect(screen.getByLabelText("Saved model")).toHaveTextContent("provider-b/x");
});

it("edits optional link permissionTier and omits it when reset to Host default", () => {
  render(<Editor />);
  const select = screen.getByLabelText("Link permission tier");
  expect(select).toHaveValue("");
  expect(screen.getByLabelText("Saved permission tier")).toHaveTextContent("none");
  fireEvent.change(select, { target: { value: "accept-edits" } });
  expect(screen.getByLabelText("Saved permission tier")).toHaveTextContent("accept-edits");
  fireEvent.change(select, { target: { value: "" } });
  expect(screen.getByLabelText("Saved permission tier")).toHaveTextContent("none");
});
