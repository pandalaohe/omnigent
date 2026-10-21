import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SelectionList, type PickerItem } from "./SelectionList";
import { resolveMulti, type MultiValue } from "@/lib/multiSelection";

vi.mock("@tanstack/react-virtual", async () => {
  const { useState: useMockState } = await import("react");
  return {
    useVirtualizer: ({ count }: { count: number }) => {
      const [first, setFirst] = useMockState(0);
      return {
        getTotalSize: () => count * 48,
        getVirtualItems: () =>
          Array.from({ length: Math.min(3, count - first) }, (_, offset) => ({
            key: first + offset,
            index: first + offset,
            start: (first + offset) * 48,
          })),
        scrollToIndex: setFirst,
      };
    },
  };
});

const items: PickerItem[] = [
  { id: "alpha", label: "Alpha" },
  { id: "beta", label: "Beta" },
  { id: "gamma", label: "Gamma" },
  { id: "archived", label: "Archived", unavailableReason: "Archived project" },
  ...Array.from({ length: 6 }, (_, index) => ({
    id: `other-${index}`,
    label: `Other ${index}`,
  })),
];

function Picker({ initial }: { initial: MultiValue }) {
  const [value, setValue] = useState(initial);
  return (
    <>
      <output data-testid="selection">{resolveMulti(value, items).join(",")}</output>
      <SelectionList
        mode="multi"
        items={items}
        value={value}
        onChange={setValue}
        searchLabel="Search projects"
      />
    </>
  );
}

afterEach(cleanup);

describe("SelectionList", () => {
  it("limits bulk actions to shown eligible rows and groups skipped projects", () => {
    render(<Picker initial={{ mode: "except", ids: ["alpha", "beta"] }} />);
    expect(screen.getByText(/skipped 1/)).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Search projects" }), {
      target: { value: "beta" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Select ▾" }));
    fireEvent.click(screen.getByRole("button", { name: "Select all shown" }));
    expect(screen.getByTestId("selection").textContent).toContain("beta");
    expect(screen.getByTestId("selection").textContent).not.toContain("alpha");
    fireEvent.change(screen.getByRole("textbox", { name: "Search projects" }), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Skipped \(1\).*Archived project/ }));
    expect(screen.getByRole("checkbox", { name: /Archived/ })).toBeDisabled();
  });

  it("keeps a separate draft when switching between Only and All except", () => {
    render(<Picker initial={{ mode: "except", ids: ["alpha"] }} />);
    fireEvent.click(screen.getByRole("button", { name: "Only these" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Gamma" }));
    expect(screen.getByTestId("selection")).toHaveTextContent("gamma");
    fireEvent.click(screen.getByRole("button", { name: "All, except these" }));
    expect(screen.getByTestId("selection").textContent).not.toContain("alpha");
    expect(screen.getByTestId("selection").textContent).toContain("beta");
  });

  it("focuses the intended virtual row after skipping a disabled item", () => {
    const many = [
      { id: "taken", label: "Taken", disabled: true },
      ...Array.from({ length: 205 }, (_, index) => ({ id: `row-${index}`, label: `Row ${index}` })),
    ];
    render(
      <SelectionList
        mode="single"
        items={many}
        value={null}
        onChange={vi.fn()}
        searchLabel="Search agents"
        searchThreshold={0}
      />,
    );
    const search = screen.getByRole("textbox", { name: "Search agents" });
    fireEvent.keyDown(search, { key: "ArrowDown" });
    expect(document.activeElement).toHaveAttribute("data-picker-item", "row-0");
    fireEvent.keyDown(document.activeElement!, { key: "ArrowDown" });
    expect(document.activeElement).toHaveAttribute("data-picker-item", "row-1");
  });

  it("uses Space for a row and Enter to confirm a multi-selection", () => {
    const confirm = vi.fn();
    function WithConfirm() {
      const [value, setValue] = useState<MultiValue>({ mode: "only", ids: [] });
      return (
        <SelectionList
          mode="multi"
          items={[{ id: "alpha", label: "Alpha" }]}
          value={value}
          onChange={setValue}
          onConfirm={confirm}
          searchLabel="Search projects"
        />
      );
    }
    render(<WithConfirm />);
    const row = screen.getByRole("checkbox", { name: "Alpha" });
    fireEvent.keyDown(row, { key: " " });
    expect(row).toHaveAttribute("aria-checked", "true");
    fireEvent.keyDown(row, { key: "Enter" });
    expect(confirm).toHaveBeenCalledOnce();
  });
});
