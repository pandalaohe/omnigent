// Integration test for project icons: the real landing tile, picker tabs,
// mutation hook and API client, with only the network and heavy picker datasets
// stubbed. Every write must merge onto the loaded config, never replace it.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ProjectLandingIcon } from "./ProjectIconPicker";
import { createProject, updateProjectConfig } from "@/lib/projectsApi";
import type { ProjectConfig } from "@/lib/projectsApi";

vi.mock("@/lib/projectsApi", () => ({
  getProject: vi.fn(),
  updateProjectConfig: vi.fn(),
  createProject: vi.fn(),
}));
vi.mock("next-themes", () => ({ useTheme: () => ({ resolvedTheme: "light" }) }));
// The real emoji-mart picker fetches a large JSON dataset that Node rejects
// under vitest; stub it to one button that reports a chosen emoji, so the
// open → select → save path is exercised without the dataset.
vi.mock("@emoji-mart/react", () => ({
  default: ({ onEmojiSelect }: { onEmojiSelect: (e: { native: string }) => void }) => (
    <button type="button" data-testid="pick-fire" onClick={() => onEmojiSelect({ native: "🔥" })}>
      🔥
    </button>
  ),
}));
vi.mock("@emoji-mart/data", () => ({ default: {} }));
vi.mock("@/components/TablerIconPicker", () => ({
  TablerIconPicker: ({ onSelect }: { onSelect: (value: string) => void }) => (
    <button type="button" data-testid="pick-rocket" onClick={() => onSelect("tabler:rocket")}>
      rocket
    </button>
  ),
}));

const updateMock = vi.mocked(updateProjectConfig);
const createMock = vi.mocked(createProject);

interface Overrides {
  projectId?: string | null;
  projectName?: string;
  config?: ProjectConfig | undefined;
  configReady?: boolean;
}

function renderIcon(overrides: Overrides = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ProjectLandingIcon
        projectId={"projectId" in overrides ? (overrides.projectId ?? null) : "p_1"}
        projectName={overrides.projectName ?? "Work"}
        config={"config" in overrides ? overrides.config : {}}
        configReady={overrides.configReady ?? true}
      />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  updateMock.mockReset();
  createMock.mockReset();
  updateMock.mockResolvedValue({ id: "p_1", name: "Work", config: {} });
  createMock.mockResolvedValue({ id: "p_new", name: "Work", config: {} });
});

afterEach(cleanup);

describe("ProjectLandingIcon", () => {
  it("merges the picked emoji onto the loaded config, preserving other defaults", async () => {
    const config: ProjectConfig = {
      host_id: "h1",
      workspace: "/repo",
      agent_id: "a1",
      use_worktree: true,
      base_branch: "main",
    };
    renderIcon({ config });

    fireEvent.click(screen.getByTestId("project-icon-tile"));
    fireEvent.click(screen.getByTestId("project-icon-tab-emoji"));
    fireEvent.click(await screen.findByTestId("pick-fire"));

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    // The whole prior config survives; only `icon` is added.
    expect(updateMock).toHaveBeenCalledWith("p_1", { ...config, icon: "🔥" });
  });

  it("selects a Tabler icon by default and preserves the loaded config", async () => {
    const config: ProjectConfig = { host_id: "h1", workspace: "/repo" };
    renderIcon({ config });

    fireEvent.click(screen.getByTestId("project-icon-tile"));
    expect(screen.getByTestId("project-icon-tab-icons")).toHaveAttribute("aria-checked", "true");
    fireEvent.click(await screen.findByTestId("pick-rocket"));

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    expect(updateMock).toHaveBeenCalledWith("p_1", { ...config, icon: "tabler:rocket" });
  });

  it("removes the icon while preserving the other defaults", async () => {
    renderIcon({ config: { host_id: "h1", icon: "🔥" } });

    fireEvent.click(screen.getByTestId("project-icon-remove"));

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    expect(updateMock).toHaveBeenCalledWith("p_1", { host_id: "h1" });
  });

  it("does not write before the config has loaded (guards the data-loss race)", async () => {
    renderIcon({ config: undefined, configReady: false });

    // The edit affordance is disabled and the tile won't open the picker, so a
    // config-wiping `{ icon }` PATCH can't be issued against unloaded state.
    expect(screen.getByTestId("project-icon-edit")).toBeDisabled();
    fireEvent.click(screen.getByTestId("project-icon-tile"));
    expect(screen.queryByTestId("project-icon-picker")).toBeNull();
    expect(updateMock).not.toHaveBeenCalled();
  });

  it("promotes a label-only folder (no id) when an icon is first set", async () => {
    renderIcon({ projectId: null, config: undefined, configReady: true });

    fireEvent.click(screen.getByTestId("project-icon-tile"));
    fireEvent.click(screen.getByTestId("project-icon-tab-emoji"));
    fireEvent.click(await screen.findByTestId("pick-fire"));

    await waitFor(() => expect(updateMock).toHaveBeenCalledTimes(1));
    // The label-only folder is promoted (created) first, then the icon is set
    // on the fresh first-class id.
    expect(createMock).toHaveBeenCalledWith("Work");
    expect(updateMock).toHaveBeenCalledWith("p_new", { icon: "🔥" });
  });
});
