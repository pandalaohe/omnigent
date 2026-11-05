import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { NewChatChoiceNotice } from "./NewChatChoiceNotice";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.clearAllMocks();
});

it("shows the server-backed notice after a reload with an empty query cache, and hides it when off", async () => {
  let sessionChoiceEnabled = true;
  vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
    const path = String(input);
    if (!path.includes("/v1/me/session-choice/notice")) {
      throw new Error(`Unexpected request: ${path}`);
    }
    return new Response(JSON.stringify({ notice: sessionChoiceEnabled ? "Plan: 4% left" : null }));
  });

  const renderNotice = (
    props: { host: string; agent: string; model?: string } = {
      host: "host-a",
      agent: "agent-a",
    },
  ) =>
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <NewChatChoiceNotice {...props} />
      </QueryClientProvider>,
    );

  renderNotice();
  expect(await screen.findByRole("note")).toHaveTextContent("4% left");
  expect(vi.mocked(authenticatedFetch).mock.calls[0][0]).toContain(
    "/v1/me/session-choice/notice?host_id=host-a&agent_id=agent-a",
  );
  expect(vi.mocked(authenticatedFetch).mock.calls[0][0]).not.toContain("model=");
  cleanup();
  sessionChoiceEnabled = false;
  vi.clearAllMocks();
  vi.mocked(authenticatedFetch).mockImplementation(
    async () => new Response(JSON.stringify({ notice: null })),
  );
  renderNotice();
  await waitFor(() => expect(screen.queryByRole("note")).not.toBeInTheDocument());
  await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledOnce());
  expect(vi.mocked(authenticatedFetch).mock.calls[0][0]).toContain("/v1/me/session-choice/notice");
});

it("asks again when the selected host, agent or model changes", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValue(
    new Response(JSON.stringify({ notice: "Plan: 4% left" })),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <NewChatChoiceNotice host="host-a" agent="agent-a" model="model-a" />
    </QueryClientProvider>,
  );
  await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledOnce());
  view.rerender(
    <QueryClientProvider client={client}>
      <NewChatChoiceNotice host="host-b" agent="agent-b" model="model-b" />
    </QueryClientProvider>,
  );
  await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(2));
  expect(vi.mocked(authenticatedFetch).mock.calls[1][0]).toContain(
    "host_id=host-b&agent_id=agent-b&model=model-b",
  );
});
