import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import {
  CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS,
  codexPermissionReadback,
  decodeCodexPermissionObservation,
} from "@/lib/codexApprovalMode";
import {
  ComposerHostTrigger,
  ComposerWorkspaceBar,
  ComposerWorkspaceTrigger,
  ComposerHarnessTrigger,
  ComposerPermissionPicker,
  ComposerSpeedPicker,
} from "./ComposerControls";
import {
  COMPOSER_COLLAPSED_LABEL_CLASS,
  COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS,
} from "./ChatComposer";

describe("shared composer controls", () => {
  it.each(["read-only", "nonowner", "offline", "busy"])(
    "opens native readback with disabled writes (%s) and returns keyboard focus",
    async () => {
      const user = userEvent.setup();
      const onSelect = vi.fn();
      const readback = codexPermissionReadback(
        { runnerId: "runner" },
        decodeCodexPermissionObservation("c1:0,1,1,2,0,1,4,0,0,1792749199,cnVubmVy"),
        { offline: false },
      );
      render(
        <ComposerPermissionPicker
          label="Permission mode"
          value={readback.value}
          options={CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS}
          selectedValue={readback.selectedValue}
          readback={readback}
          disabled
          disabledReason="Changes disabled"
          onSelect={onSelect}
        />,
      );
      const chip = screen.getByRole("button", { name: "Permission mode: Custom" });
      await user.tab();
      expect(chip).toHaveFocus();
      await user.keyboard("{Enter}");
      const menu = screen.getByRole("menu");
      expect(menu).toHaveClass("max-w-[calc(100vw-2rem)]");
      expect(screen.getByText("Sandbox")).toBeInTheDocument();
      expect(screen.getByText("read-only")).toBeInTheDocument();
      expect(screen.getByText("never")).toBeInTheDocument();
      expect(screen.getByText("Not reported")).toBeInTheDocument();
      expect(screen.getByText("Changes disabled")).toBeInTheDocument();
      const rows = screen.getAllByRole("menuitemradio");
      expect(rows).toHaveLength(4);
      for (const row of rows) {
        expect(row).toHaveAttribute("aria-disabled", "true");
        fireEvent.click(row);
      }
      expect(onSelect).not.toHaveBeenCalled();
      await user.keyboard("{Escape}");
      expect(screen.queryByRole("menu")).toBeNull();
      expect(chip).toHaveFocus();
    },
  );
  it("toggles fast directly with distinct icons and no visible label or menu", () => {
    const onSelect = vi.fn();
    const options = [
      { value: "standard", label: "Standard" },
      { value: "fast", label: "Fast" },
    ];
    const { rerender } = render(
      <ComposerSpeedPicker value="standard" options={options} onSelect={onSelect} />,
    );
    const trigger = screen.getByRole("button", { name: "Speed: Standard" });
    expect(trigger).toHaveAttribute("aria-pressed", "false");
    expect(trigger).toHaveAttribute("title", "Speed: Standard — use Fast");
    expect(trigger).toHaveTextContent(/^$/);
    expect(trigger.querySelector("svg.lucide-zap-off")).toBeInTheDocument();
    fireEvent.click(trigger);
    expect(onSelect).toHaveBeenCalledWith("fast");
    expect(screen.queryByRole("menu")).toBeNull();
    rerender(<ComposerSpeedPicker value="fast" options={options} onSelect={onSelect} />);
    expect(trigger).toHaveAttribute("aria-pressed", "true");
    expect(trigger.querySelector("svg.lucide-zap")).toBeInTheDocument();
    fireEvent.click(trigger);
    expect(onSelect).toHaveBeenCalledWith("standard");
  });

  it.each([
    ["standard", "Standard"],
    ["fast", "Fast"],
    ["ultrafast", "Ultrafast"],
  ])(
    "opens all advertised tiers with %s checked and a distinct non-standard icon",
    (value, label) => {
      const onSelect = vi.fn();
      const options = [
        { value: "standard", label: "Standard" },
        { value: "fast", label: "Fast" },
        { value: "ultrafast", label: "Ultrafast" },
      ];
      render(<ComposerSpeedPicker value={value} options={options} onSelect={onSelect} />);
      const trigger = screen.getByTestId("composer-speed-trigger");
      expect(trigger).toHaveAttribute("aria-label", `Speed: ${label}`);
      expect(trigger).not.toHaveAttribute("aria-pressed");
      expect(trigger).toHaveTextContent(/^$/);
      expect(
        trigger.querySelector(value === "standard" ? "svg.lucide-zap-off" : "svg.lucide-zap"),
      ).toBeInTheDocument();
      fireEvent.keyDown(trigger, { key: "ArrowDown" });
      for (const option of options) {
        expect(screen.getByRole("menuitemradio", { name: option.label })).toHaveAttribute(
          "aria-checked",
          String(option.value === value),
        );
      }
      fireEvent.click(
        screen.getByRole("menuitemradio", { name: value === "ultrafast" ? "Fast" : "Ultrafast" }),
      );
      expect(onSelect).toHaveBeenCalledWith(value === "ultrafast" ? "fast" : "ultrafast");
    },
  );

  it("offers non-fast tiers through a menu even when there are only two choices", () => {
    const onSelect = vi.fn();
    render(
      <ComposerSpeedPicker
        value="ultrafast"
        options={[
          { value: "standard", label: "Standard" },
          { value: "ultrafast", label: "Ultrafast" },
        ]}
        onSelect={onSelect}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Speed: Ultrafast" });
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Standard" }));
    expect(onSelect).toHaveBeenCalledWith("standard");
  });

  it("hides speed when the model only offers standard", () => {
    render(
      <ComposerSpeedPicker
        value="standard"
        options={[{ value: "standard", label: "Standard" }]}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("composer-speed-trigger")).toBeNull();
  });

  it("does not toggle while disabled", () => {
    const onSelect = vi.fn();
    render(
      <ComposerSpeedPicker
        value="standard"
        options={[
          { value: "standard", label: "Standard" },
          { value: "fast", label: "Fast" },
        ]}
        disabled
        onSelect={onSelect}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Speed: Standard" });
    expect(trigger).toBeDisabled();
    fireEvent.click(trigger);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("explains why a running Codex session cannot switch into Read Only", () => {
    const onSelect = vi.fn();
    render(
      <ComposerPermissionPicker
        label="Permission mode"
        value="Ask for approval"
        selectedValue="ask-for-approval"
        options={[
          { value: "ask-for-approval", label: "Ask for approval" },
          { value: "read-only", label: "Read Only" },
        ]}
        disabledOptions={["read-only"]}
        onSelect={onSelect}
      />,
    );
    fireEvent.keyDown(screen.getByRole("button", { name: "Permission mode: Ask for approval" }), {
      key: "ArrowDown",
    });
    const readOnly = screen.getByTestId("composer-permission-option-read-only");
    expect(readOnly).toHaveAttribute("data-disabled");
    expect(readOnly).toHaveTextContent("Start a new session");
    fireEvent.click(readOnly);
    expect(onSelect).not.toHaveBeenCalled();
  });
  it("uses the same workspace header and host geometry in either context", () => {
    render(
      <>
        <ComposerWorkspaceBar data-testid="workspace-bar">
          <ComposerWorkspaceTrigger kind="directory" label="repo" />
          <ComposerWorkspaceTrigger kind="worktree" label="main" />
        </ComposerWorkspaceBar>
        <ComposerHostTrigger label="This machine" status="online" />
      </>,
    );
    expect(screen.getByRole("button", { name: "repo" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "main" })).toBeInTheDocument();
    expect(screen.getByTestId("workspace-bar")).toHaveClass(
      "items-center",
      "py-0.5",
      "h-7",
      "md:h-[37px]",
      "md:py-1.5",
      "gap-0.5",
      "md:gap-2",
    );
    expect(screen.getByTestId("workspace-bar")).not.toHaveClass("items-start", "pt-1.5");
    expect(screen.getByRole("button", { name: "This machine" })).toHaveClass("w-11", "md:h-7");
  });

  it("lets workspace labels use half the bar instead of a fixed pixel cap", () => {
    render(
      <ComposerWorkspaceBar>
        <ComposerWorkspaceTrigger kind="directory" label="new-composer-width" />
        <ComposerWorkspaceTrigger kind="worktree" label="feature/new-composer-width" />
      </ComposerWorkspaceBar>,
    );

    for (const trigger of screen.getAllByRole("button")) {
      expect(trigger).toHaveClass("min-w-10", "md:min-w-11", "px-1", "max-w-[calc(50%-0.25rem)]");
      expect(trigger).not.toHaveClass("max-w-[180px]");
      expect(trigger.querySelector("span")).toHaveClass("min-w-0", "truncate");
      for (const icon of trigger.querySelectorAll("svg")) {
        expect(icon).toHaveClass("shrink-0");
      }
    }
  });

  it("renders a product-icon model trigger instead of a separate settings gear", () => {
    render(
      <ComposerHarnessTrigger
        label="Codex configuration"
        model="GPT-5.6-Sol"
        effort="High"
        icon={<span data-testid="product-icon" />}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Codex configuration" });
    expect(trigger).toHaveTextContent("GPT-5.6-Sol");
    expect(trigger).toHaveClass("w-auto");
    expect(trigger).toHaveClass("px-2", "py-0", "border-0", "leading-5", "md:min-h-7");
    expect(trigger).not.toHaveClass("pr-0");
    expect(trigger).not.toHaveClass("max-w-[7.25rem]", "md:max-w-40");
    expect(trigger).toHaveTextContent("High");
    expect(screen.getByTestId("composer-agent-model-value")).toHaveClass("truncate");
    expect(screen.getByTestId("composer-agent-model-value")).toHaveAttribute(
      "title",
      "GPT-5.6-Sol",
    );
    expect(screen.getByTestId("composer-agent-effort-value")).not.toHaveClass("hidden");
    expect(screen.getByTestId("product-icon")).toBeInTheDocument();
    expect(screen.getByTestId("composer-agent-config-value")).toHaveClass(
      COMPOSER_COLLAPSED_LABEL_CLASS,
    );
  });

  it("keeps an icon-less model label visible when the action row collapses", () => {
    render(<ComposerHarnessTrigger label="Agent" model="No agents" />);
    expect(screen.getByTestId("composer-agent-config-value")).not.toHaveClass(
      COMPOSER_COLLAPSED_LABEL_CLASS,
    );
  });

  it("keeps the model trigger clickable with an accessible, mobile-visible loading spinner", () => {
    const onClick = vi.fn();
    render(
      <ComposerHarnessTrigger
        label="Configure session"
        model=""
        icon={<span>Harness</span>}
        loading
        pending
        onClick={onClick}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Configure session" });
    const spinner = screen.getByRole("status", { name: "Loading model" });
    expect(trigger).toBeEnabled();
    expect(trigger).toHaveAttribute("aria-busy", "true");
    expect(spinner.parentElement).toBe(trigger);
    expect(screen.getByTestId("composer-agent-config-value")).not.toContainElement(spinner);
    expect(screen.queryByLabelText("Model change pending")).toBeNull();
    fireEvent.click(trigger);
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("dispatches permission selections through the caller's handler", () => {
    const onSelect = vi.fn();
    render(
      <ComposerPermissionPicker
        label="Permission mode"
        value="Bypass permissions"
        options={[
          { value: "manual", label: "Manual" },
          { value: "plan", label: "Plan" },
        ]}
        onSelect={onSelect}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Permission mode: Bypass permissions" });
    for (const forbidden of ["hidden", "max-w-20"]) {
      expect(screen.getByText("Bypass permissions")).not.toHaveClass(forbidden);
    }
    expect(screen.getByText("Bypass permissions")).toHaveClass(COMPOSER_COLLAPSED_LABEL_CLASS);
    expect(trigger).toHaveClass("w-auto", "gap-1", "px-2");
    fireEvent.keyDown(trigger, {
      key: "ArrowDown",
    });
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Plan" }));
    expect(onSelect).toHaveBeenCalledWith("plan");
  });

  it("marks the selected permission mode and leaves the rest unchecked", () => {
    render(
      <ComposerPermissionPicker
        label="Permission mode"
        value="Auto"
        selectedValue="auto"
        options={[
          { value: "default", label: "Manual" },
          { value: "auto", label: "Auto" },
          { value: "plan", label: "Plan" },
        ]}
        onSelect={vi.fn()}
      />,
    );
    fireEvent.keyDown(screen.getByRole("button", { name: "Permission mode: Auto" }), {
      key: "ArrowDown",
    });
    const selectedRow = screen.getByTestId("composer-permission-option-auto");
    expect(selectedRow).toHaveAttribute("aria-checked", "true");
    expect(
      selectedRow.querySelector('[data-slot="dropdown-menu-radio-item-indicator"]'),
    ).toBeInTheDocument();
    for (const other of ["default", "plan"]) {
      expect(screen.getByTestId(`composer-permission-option-${other}`)).toHaveAttribute(
        "aria-checked",
        "false",
      );
    }
  });

  it("marks no permission mode when the selected value is unknown", () => {
    render(
      <ComposerPermissionPicker
        label="Permission mode"
        value="Permission mode"
        options={[
          { value: "default", label: "Manual" },
          { value: "auto", label: "Auto" },
        ]}
        onSelect={vi.fn()}
      />,
    );
    fireEvent.keyDown(screen.getByRole("button", { name: "Permission mode: Permission mode" }), {
      key: "ArrowDown",
    });
    for (const mode of ["default", "auto"]) {
      expect(screen.getByTestId(`composer-permission-option-${mode}`)).toHaveAttribute(
        "aria-checked",
        "false",
      );
    }
  });

  it("exposes shared permission concepts without changing harness wire values", () => {
    const { rerender } = render(
      <ComposerPermissionPicker
        label="Permission mode"
        value="Plan"
        harness="claude-native"
        selectedValue="plan"
        options={[{ value: "plan", label: "Plan" }]}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Permission mode: Plan" })).toHaveAttribute(
      "data-permission-concept",
      "read-only",
    );

    rerender(
      <ComposerPermissionPicker
        label="Permission mode"
        value="Read only"
        harness="codex-native"
        selectedValue="read-only"
        options={[{ value: "default", label: "Automatic" }]}
        onSelect={vi.fn()}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Permission mode: Read only" });
    expect(trigger).toHaveAttribute("data-permission-concept", "read-only");
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    expect(screen.getByRole("menuitemradio", { name: "Automatic" })).toHaveAttribute(
      "data-permission-concept",
      "automatic",
    );
  });

  it.each([false, true])(
    "restores permission focus unless another menu has opened (next menu=%s)",
    async (nextMenuOpen) => {
      vi.useFakeTimers();
      try {
        render(
          <>
            <ComposerPermissionPicker
              label="Permission mode"
              value="Manual"
              options={[{ value: "plan", label: "Plan" }]}
              onSelect={vi.fn()}
            />
            <div role="menu" data-state="open" tabIndex={-1} data-testid="next-menu" />
          </>,
        );
        const trigger = screen.getByRole("button", { name: "Permission mode: Manual" });
        fireEvent.keyDown(trigger, { key: "ArrowDown" });
        fireEvent.click(screen.getByRole("menuitemradio", { name: "Plan" }));
        const nextMenu = screen.getByTestId("next-menu");
        if (nextMenuOpen) nextMenu.focus();

        await act(() => vi.runOnlyPendingTimersAsync());

        expect(nextMenuOpen ? nextMenu : trigger).toHaveFocus();
      } finally {
        vi.useRealTimers();
      }
    },
  );

  it("keeps cached permissions readable but inert while their live configuration loads", () => {
    const onSelect = vi.fn();
    render(
      <ComposerPermissionPicker
        label="Permission mode"
        value="Plan"
        options={[{ value: "plan", label: "Plan" }]}
        loading
        onSelect={onSelect}
      />,
    );
    const trigger = screen.getByRole("button", { name: "Permission mode: Plan" });
    expect(trigger).toBeDisabled();
    expect(trigger).toHaveAttribute("aria-busy", "true");
    expect(trigger).toHaveClass("disabled:opacity-100");
    fireEvent.pointerDown(trigger, { button: 0 });
    fireEvent.click(trigger);
    expect(screen.queryByRole("menu")).toBeNull();
    expect(onSelect).not.toHaveBeenCalled();
  });

  it.each([false, true])(
    "allows cached permission choices while refreshing unless explicitly disabled (%s)",
    (disabled) => {
      const onSelect = vi.fn();
      render(
        <ComposerPermissionPicker
          label="Permission mode"
          value="Default"
          options={[{ value: "plan", label: "Plan" }]}
          loading
          interactiveWhileLoading
          disabled={disabled}
          onSelect={onSelect}
        />,
      );
      const trigger = screen.getByRole("button", { name: "Permission mode: Default" });
      expect(trigger).toHaveAttribute("aria-busy", "true");
      if (disabled) {
        expect(trigger).toBeDisabled();
        fireEvent.pointerDown(trigger, { button: 0 });
        expect(screen.queryByRole("menu")).toBeNull();
      } else {
        expect(trigger).toBeEnabled();
        fireEvent.keyDown(trigger, { key: "ArrowDown" });
        fireEvent.click(screen.getByRole("menuitemradio", { name: "Plan" }));
        expect(onSelect).toHaveBeenCalledWith("plan");
      }
    },
  );
});

describe("workspace bar label collapse", () => {
  class StubResizeObserver {
    static callbacks: ResizeObserverCallback[] = [];
    constructor(callback: ResizeObserverCallback) {
      StubResizeObserver.callbacks.push(callback);
    }
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  const fireResize = () => {
    for (const callback of StubResizeObserver.callbacks) callback([], {} as ResizeObserver);
  };

  afterEach(() => {
    StubResizeObserver.callbacks = [];
    vi.unstubAllGlobals();
  });

  // jsdom does no layout, so stand in for it with fixed scroll/client widths.
  const defineWidth = (element: HTMLElement, clientWidth: number, scrollWidth: number) => {
    Object.defineProperty(element, "clientWidth", { configurable: true, get: () => clientWidth });
    Object.defineProperty(element, "scrollWidth", { configurable: true, get: () => scrollWidth });
  };

  it("marks each chip label to collapse to its icon and keeps the label as the name", () => {
    render(
      <ComposerWorkspaceBar>
        <ComposerWorkspaceTrigger kind="directory" label="repo" />
        <ComposerWorkspaceTrigger kind="worktree" label="main" aria-label="Branch: main" />
      </ComposerWorkspaceBar>,
    );
    const label = screen.getByText("repo");
    expect(label).toHaveAttribute("data-workspace-collapse-label");
    expect(label).toHaveClass(COMPOSER_WORKSPACE_COLLAPSED_LABEL_CLASS);
    // The name survives the text hiding; a caller's explicit name still wins.
    expect(screen.getByRole("button", { name: "repo" })).toHaveAttribute("aria-label", "repo");
    expect(screen.getByRole("button", { name: "Branch: main" })).toBeInTheDocument();
  });

  it("collapses the labels to icons once a chip can no longer show its full text", () => {
    vi.stubGlobal("ResizeObserver", StubResizeObserver);
    render(
      <ComposerWorkspaceBar data-testid="bar">
        <ComposerWorkspaceTrigger kind="worktree" label="feature/really-long-branch-name" />
      </ComposerWorkspaceBar>,
    );
    const bar = screen.getByTestId("bar");
    const label = bar.querySelector<HTMLElement>("[data-workspace-collapse-label]")!;
    defineWidth(bar, 200, 200);
    // The label's text is wider than the box it was given: it is truncating.
    defineWidth(label, 40, 120);
    fireResize();
    expect(bar).toHaveAttribute("data-labels", "collapsed");
    // Given room for the full text again, the labels come back.
    defineWidth(label, 120, 120);
    fireResize();
    expect(bar).not.toHaveAttribute("data-labels");
  });

  it("collapses when the row overflows even if no single label is truncating", () => {
    vi.stubGlobal("ResizeObserver", StubResizeObserver);
    render(
      <ComposerWorkspaceBar data-testid="bar">
        <ComposerWorkspaceTrigger kind="directory" label="repo" />
      </ComposerWorkspaceBar>,
    );
    const bar = screen.getByTestId("bar");
    const label = bar.querySelector<HTMLElement>("[data-workspace-collapse-label]")!;
    defineWidth(label, 40, 40);
    defineWidth(bar, 150, 300);
    fireResize();
    expect(bar).toHaveAttribute("data-labels", "collapsed");
  });
});
