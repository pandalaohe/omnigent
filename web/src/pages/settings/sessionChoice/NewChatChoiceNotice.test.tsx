import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import {
  SESSION_COLLAB_STORAGE_KEY,
  SESSION_COLLAB_CHANGED_EVENT,
} from "@/lib/sessionCollabPreferences";
import { authenticatedFetch } from "@/lib/identity";
import { NewChatChoiceNotice } from "./NewChatChoiceNotice";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.clearAllMocks();
});

it("uses the synced switch without a settings request, and starts just the notice read when enabled", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <NewChatChoiceNotice host="host-a" agent="agent-a" model="model-a" />
    </QueryClientProvider>,
  );
  await act(async () => {});
  expect(authenticatedFetch).not.toHaveBeenCalled();
  let finish!: (response: Response) => void;
  vi.mocked(authenticatedFetch).mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  act(() => {
    localStorage.setItem(
      SESSION_COLLAB_STORAGE_KEY,
      JSON.stringify({ sessionChoice: { enabled: true } }),
    );
    window.dispatchEvent(new Event(SESSION_COLLAB_CHANGED_EVENT));
  });
  await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledOnce());
  expect(vi.mocked(authenticatedFetch).mock.calls[0][0]).toBe(
    "/v1/me/session-choice/notice?host_id=host-a&agent_id=agent-a&model=model-a",
  );
  expect(screen.queryByRole("note")).not.toBeInTheDocument();
  await act(async () => finish(new Response(JSON.stringify({ notice: "Plan: 4% left" }))));
  expect(await screen.findByRole("note")).toHaveTextContent("4% left");
  act(() =>
    client.setQueryData(["sessionChoice"], { settings: { enabled: false }, revision: "off" }),
  );
  await waitFor(() => expect(screen.queryByRole("note")).not.toBeInTheDocument());
  expect(authenticatedFetch).toHaveBeenCalledOnce();
});
