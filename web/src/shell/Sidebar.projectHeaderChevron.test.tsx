import { renderSidebar as renderSidebarAt } from "@/test/sidebarTestHelpers";
import { conversationPage } from "@/test/sidebarMockHelpers";
import { PROJECT_KANBAN_BOARD_STORAGE_KEY } from "@/lib/projectKanbanPreferences";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useScopeCache", () => import("@/test/mockScopeCache"));
// Project names select the new-session target. A separate, always-visible
// chevron expands the project without changing that selection. The Projects
// group itself retains its existing hover-revealed trailing chevron.

import { cleanup, fireEvent, screen, within } from "@testing-library/react";

vi.mock("@/hooks/useConversations", async () => {
  const { conversationHooksMock } = await import("@/test/sidebarMockHelpers");
  return {
    ...conversationHooksMock(),
    useProjects: () => ({ data: [{ id: "p_my", name: "My Project" }] }),
  };
});

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";

const useConvMock = vi.mocked(useConversations);

function mockConversations(conversations: Conversation[]) {
  const withData = conversationPage(conversations);
  useConvMock.mockImplementation(() => withData);
}

function renderSidebar(initialEntry = "/") {
  return renderSidebarAt({ route: initialEntry });
}

/** The <button> header for a section/folder, found by its accessible name. */
function headerButton(name: string): HTMLElement {
  return screen.getByRole("button", { name });
}

/** SVG elements expose `className` as an SVGAnimatedString, not a string;
 *  read the raw class attribute instead. */
function classOf(el: Element): string {
  return el.getAttribute("class") ?? "";
}

beforeEach(() => {
  localStorage.clear();
  mockConversations([]);
});

afterEach(() => {
  cleanup();
});

describe("project folder header icon/chevron", () => {
  it("keeps the target's folder icon visible beside the independent toggle", () => {
    renderSidebar();
    const header = headerButton("Use My Project for new sessions");

    // Project folders are real rows, not muted section labels: use the shared
    // text-ui compact treatment with 8px insets/gap and foreground text.
    expect(header).toHaveClass(
      "sidebar-row",
      "h-auto",
      "min-h-0",
      "gap-2",
      "rounded-[var(--radius-otto-button)]",
      "pl-8",
      "py-1.5",
      "md:py-1",
      "sidebar-compact-text",
      "text-foreground",
    );
    // Project names sit heavier than the regular-weight session rows below.
    expect(within(header).getByText("My Project")).toHaveClass("font-medium");

    const folder = header.querySelector(".lucide-folder") as HTMLElement;
    expect(folder).not.toBeNull();
    expect(folder).toHaveClass("text-muted-foreground");

    // Hovering the target must not hide its icon: the chevron has its own button.
    const folderWrapper = folder.parentElement as HTMLElement;
    expect(classOf(folderWrapper)).not.toMatch(/opacity-0/);
    expect(header.querySelector(".lucide-chevron-right")).toBeNull();
    expect(header.contains(headerButton("My Project"))).toBe(false);
  });

  it("keeps the independent project chevron visible without hover", () => {
    renderSidebar();
    const header = headerButton("My Project");

    const chevrons = Array.from(header.querySelectorAll(".lucide-chevron-right"));
    expect(chevrons).toHaveLength(1);
    const trailing = chevrons.find((c) => !classOf(c).includes("absolute")) as HTMLElement;
    expect(trailing).toBeTruthy();
    expect(classOf(trailing)).not.toMatch(/hidden|opacity-0/);
    expect(classOf(trailing)).not.toMatch(/md:group-hover:opacity-100/);
  });

  it("lists a highlighted ghost row under the project for a project-scoped composer", () => {
    renderSidebar("/?project=My%20Project");

    const header = headerButton("Use My Project for new sessions");
    expect(header).toHaveAttribute("aria-pressed", "true");
    const ghost = within(header.closest("section")!).getByRole("link", { name: /^New session$/ });
    expect(ghost).toHaveAttribute("href", "/?project=My%20Project");
    expect(ghost).toHaveClass(
      "bg-[var(--sidebar-active)]",
      "text-[var(--sidebar-active-foreground)]",
    );

    const newSession = screen.getByTestId("new-chat-button");
    expect(newSession).not.toHaveClass("bg-[var(--sidebar-active)]");
    expect(newSession.querySelector("svg")).toHaveClass("text-muted-foreground");
  });

  it("shows a left-aligned empty-project message and new-session action", () => {
    renderSidebar();
    fireEvent.click(headerButton("My Project"));

    const action = screen
      .getAllByRole("link", { name: "new session" })
      .find((link) => link.getAttribute("href") === "/?project=My%20Project")!;
    const empty = action.closest(".sidebar-row")!;
    expect(empty.parentElement).toHaveClass("pl-6");
    expect(empty).toHaveClass(
      "sidebar-row",
      "sidebar-compact-text",
      "h-auto",
      "min-h-0",
      "w-full",
      "px-2",
      "py-1.5",
      "md:py-1",
      "justify-center",
      "rounded-[var(--radius-otto-button)]",
      "text-left",
      "text-muted-foreground",
    );
    expect(empty).not.toHaveClass(
      "ml-8",
      "mr-2",
      "border",
      "border-dashed",
      "items-center",
      "text-center",
      "px-0",
      "pb-2",
      "md:pb-2",
    );
    const message = empty.querySelector("p")!;
    expect(message).not.toHaveClass("text-ui");
    const body = message.querySelector("span")!;
    expect(body).not.toHaveClass("text-ui");
    expect(body).toHaveTextContent("No sessions. Start a new session.");
    expect(body).toContainElement(action);
    expect(message).toContainElement(action);
    expect(within(empty as HTMLElement).getByRole("link", { name: "new session" })).toBe(action);
    expect(action).toHaveAttribute("href", "/?project=My%20Project");
    expect(action).toHaveClass("text-primary", "hover:underline");
    expect(action).not.toHaveAttribute("data-slot", "button");
    expect(action.querySelector("svg")).toBeNull();
  });

  it("leaves iconless section headers with a hover-revealed trailing chevron and no swap", () => {
    renderSidebar();
    // The "Projects" group header carries no leading icon.
    const header = headerButton("Projects");

    // The parent section label uses the settings-scaled subtitle tier.
    expect(header).toHaveClass("gap-1", "pl-2", "text-sm", "font-normal");
    expect(header).not.toHaveClass("font-medium", "uppercase");

    expect(header.querySelector(".lucide-folder")).toBeNull();

    const chevrons = Array.from(header.querySelectorAll(".lucide-chevron-right"));
    // Exactly one chevron (no in-slot swap), and it is the classic
    // desktop-hover-revealed trailing caret — not mobile-only.
    expect(chevrons).toHaveLength(1);
    const [chevron] = chevrons;
    expect(classOf(chevron)).not.toMatch(/\babsolute\b/);
    expect(classOf(chevron)).not.toMatch(/md:hidden/);
    expect(classOf(chevron)).toMatch(/md:group-hover:opacity-100/);
  });
});

it("opens the project home from the row and expands or collapses it only from the chevron", () => {
  localStorage.clear();
  renderSidebarAt({ features: { project_kanban: true } });
  const header = headerButton("Open My Project board");
  const chevron = headerButton("My Project");
  expect(chevron).toHaveAttribute("aria-expanded", "false");

  fireEvent.click(header);
  expect(chevron).toHaveAttribute("aria-expanded", "false");
  expect(header).toHaveAttribute("aria-current", "page");
  expect(screen.getByTestId("new-chat-button")).not.toHaveClass("bg-[var(--sidebar-active)]");

  fireEvent.click(chevron);
  expect(chevron).toHaveAttribute("aria-expanded", "true");
  expect(
    within(header.closest("section")!).queryByRole("link", { name: /^New session$/ }),
  ).toBeNull();
  fireEvent.click(header);
  expect(chevron).toHaveAttribute("aria-expanded", "true");
  fireEvent.click(chevron);
  expect(chevron).toHaveAttribute("aria-expanded", "false");
  expect(header).toHaveAttribute("aria-current", "page");
});

it("keeps the project target and its collapse toggle when the board is turned off in Settings", () => {
  localStorage.clear();
  localStorage.setItem(PROJECT_KANBAN_BOARD_STORAGE_KEY, "false");
  renderSidebarAt({ features: { project_kanban: true } });
  const header = headerButton("Use My Project for new sessions");
  const chevron = headerButton("My Project");

  fireEvent.click(header);
  expect(header).toHaveAttribute("aria-pressed", "true");
  expect(header).not.toHaveAttribute("aria-current");
  expect(chevron).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(chevron);
  expect(chevron).toHaveAttribute("aria-expanded", "true");
  localStorage.clear();
});

it("opens New project without toggling the Projects header", () => {
  renderSidebar();
  const projects = headerButton("Projects");
  const expanded = projects.getAttribute("aria-expanded");
  fireEvent.click(projects);
  expect(projects.getAttribute("aria-expanded")).not.toBe(expanded);
  const afterToggle = projects.getAttribute("aria-expanded");
  fireEvent.click(screen.getByRole("button", { name: "New project" }));
  expect(screen.getByRole("dialog", { name: "New project" })).toBeVisible();
  expect(projects.getAttribute("aria-expanded")).toBe(afterToggle);
});
