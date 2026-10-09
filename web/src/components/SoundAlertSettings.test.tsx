import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

// Radix Select uses a portal + pointer events jsdom can't drive; a native
// <select> lets the tests drive the sound choice directly.
vi.mock("@/components/ui/select", async () => {
  const { Children, isValidElement } = await import("react");
  const SelectTrigger = ({ children }: { children?: ReactNode }) => children;
  const Select = ({
    value,
    onValueChange,
    children,
  }: {
    value: string;
    onValueChange: (value: string) => void;
    children: ReactNode;
  }) => {
    const kids = Children.toArray(children);
    const trigger = kids.find((child) => isValidElement(child) && child.type === SelectTrigger);
    const testId =
      isValidElement(trigger) && trigger.props && typeof trigger.props === "object"
        ? (trigger.props as Record<string, unknown>)["data-testid"]
        : undefined;
    return (
      <select
        data-testid={typeof testId === "string" ? testId : undefined}
        value={value}
        onChange={(event) => onValueChange(event.target.value)}
      >
        {kids.filter((child) => !(isValidElement(child) && child.type === SelectTrigger))}
      </select>
    );
  };
  return {
    Select,
    SelectTrigger,
    SelectValue: () => null,
    SelectContent: ({ children }: { children: ReactNode }) => children,
    SelectItem: ({ value, children }: { value: string; children: ReactNode }) => (
      <option value={value}>{children}</option>
    ),
  };
});

import { SoundAlertSettings } from "./SoundAlertSettings";
import { SOUND_ALERTS_STORAGE_KEY } from "@/lib/soundAlertPreferences";

interface StoredPreferences {
  levels: Record<string, { enabled: boolean; sound: string }>;
}

function stored(): StoredPreferences {
  return JSON.parse(localStorage.getItem(SOUND_ALERTS_STORAGE_KEY) ?? "null") as StoredPreferences;
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => cleanup());

describe("SoundAlertSettings", () => {
  it("writes a level switch to the shared namespace", () => {
    render(<SoundAlertSettings />);
    const toggle = screen.getByTestId("sound-alert-level-done");
    expect(toggle).toHaveAttribute("aria-checked", "true");

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(stored().levels.done.enabled).toBe(false);
    expect(stored().levels.error.enabled).toBe(true);
  });

  it("writes a sound choice from the select", () => {
    render(<SoundAlertSettings />);

    fireEvent.change(screen.getByTestId("sound-alert-sound-done"), { target: { value: "pop" } });

    expect(stored().levels.done.sound).toBe("pop");
  });
});
