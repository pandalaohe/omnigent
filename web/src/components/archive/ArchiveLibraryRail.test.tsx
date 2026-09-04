import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import {
  useArchivedConversations,
  useArchivedSessionFacets,
  useProjects,
} from "@/hooks/useConversations";
import { useHosts } from "@/hooks/useHosts";
import { ArchiveLibraryRail } from "./ArchiveLibraryRail";

vi.mock("@/hooks/useConversations", () => ({
  useArchivedConversations: vi.fn(),
  useArchivedSessionFacets: vi.fn(),
  useProjects: vi.fn(),
}));
vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn() }));
vi.mock("@/components/archive/ArchiveTranscriptViewer", () => ({
  ArchiveTranscriptViewer: ({ conversation: selected }: { conversation: Conversation | null }) => (
    <div data-testid="transcript-stub">{selected?.title ?? "none"}</div>
  ),
}));

const useArchivedMock = vi.mocked(useArchivedConversations);
const useFacetsMock = vi.mocked(useArchivedSessionFacets);
const useHostsMock = vi.mocked(useHosts);
const useProjectsMock = vi.mocked(useProjects);

function archiveConversation(id: string, title: string): Conversation {
  return {
    id,
    object: "conversation",
    title,
    created_at: 1,
    updated_at: 1,
    archived: true,
    labels: {},
    permission_level: null,
    agent_name: "codex",
    host_id: "host-win",
  };
}

describe("ArchiveLibraryRail", () => {
  beforeEach(() => {
    localStorage.clear();
    useFacetsMock.mockReturnValue({
      data: { projects: [], hostIds: [], agentNames: [] },
    } as unknown as ReturnType<typeof useArchivedSessionFacets>);
    useHostsMock.mockReturnValue({
      data: [{ host_id: "host-win", name: "Windows" }],
    } as ReturnType<typeof useHosts>);
    useProjectsMock.mockReturnValue({ data: [] } as unknown as ReturnType<typeof useProjects>);
    const firstPage = {
      data: {
        data: [archiveConversation("conv-1", "First archive page")],
        first_id: "conv-1",
        last_id: "cursor-1",
        has_more: true,
      },
      isLoading: false,
      isFetching: false,
    } as ReturnType<typeof useArchivedConversations>;
    const secondPage = {
      data: {
        data: [archiveConversation("conv-2", "Second archive page")],
        first_id: "conv-2",
        last_id: "conv-2",
        has_more: false,
      },
      isLoading: false,
      isFetching: false,
    } as ReturnType<typeof useArchivedConversations>;
    useArchivedMock.mockImplementation((_filters, after) => {
      return after === "cursor-1" ? secondPage : firstPage;
    });
  });

  it("pages through a large archive and can return to the prior page", async () => {
    render(<ArchiveLibraryRail />);

    expect(await screen.findByRole("option", { name: /First archive page/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    expect(await screen.findByRole("option", { name: /Second archive page/ })).toBeInTheDocument();
    expect(screen.getByText("Page 2")).toBeInTheDocument();
    expect(screen.getByTestId("transcript-stub")).toHaveTextContent("Second archive page");

    fireEvent.click(screen.getByRole("button", { name: /Previous/ }));
    await waitFor(() =>
      expect(screen.getByRole("option", { name: /First archive page/ })).toBeInTheDocument(),
    );
    expect(screen.getByText("Page 1")).toBeInTheDocument();
  });

  it("includes first-class projects that are absent from legacy archive facets", () => {
    useProjectsMock.mockReturnValue({
      data: [{ id: "project-1", name: "First class project" }],
    } as unknown as ReturnType<typeof useProjects>);

    render(<ArchiveLibraryRail />);

    expect(screen.getByRole("option", { name: "First class project" })).toBeInTheDocument();
  });
});
