import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { BuiltinPromptEditor } from "./BuiltinPromptEditor";
import { SessionPromptField } from "./SessionPromptField";

const fetch = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetch }));
afterEach(() => {
  cleanup();
  fetch.mockReset();
});

it("saves an empty version, reports built-in drift, and supports restore with undo", async () => {
  const digest = "a".repeat(64);
  let saved: { text: string; base_sha256: string } | null = {
    text: "Saved prompt",
    base_sha256: "b".repeat(64),
  };
  fetch.mockImplementation(async (_url, options) => {
    if (options?.method === "PATCH") {
      const patch = JSON.parse(options.body);
      saved = patch.text === null ? null : patch;
    }
    return new Response(
      JSON.stringify({
        text: saved?.text ?? "Built-in prompt",
        builtin_text: "Built-in prompt",
        base_sha256: digest,
        saved_base_sha256: saved?.base_sha256 ?? null,
        customized: saved !== null,
        builtin_changed: saved !== null && saved.base_sha256 !== digest,
      }),
      { headers: { "Content-Type": "application/json" } },
    );
  });
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <BuiltinPromptEditor agentId="agent-one" />
    </QueryClientProvider>,
  );
  const input = await screen.findByRole("textbox", { name: "Starting prompt" });
  expect(screen.getByText("The built-in changed since you edited this.")).toBeInTheDocument();
  fireEvent.change(input, { target: { value: "" } });
  fireEvent.click(screen.getByRole("button", { name: "Save prompt" }));
  await screen.findByText("Starting prompt saved.");
  expect(saved?.text).toBe("");
  fireEvent.click(screen.getByRole("button", { name: "Restore built-in" }));
  await screen.findByText("Built-in restored.");
  expect(input).toHaveValue("Built-in prompt");
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  await screen.findByText("Starting prompt restored.");
  expect(input).toHaveValue("");
  expect(
    screen.getByText("New chats use this. A running chat picks it up after it reconnects."),
  ).toBeInTheDocument();
});

it("keeps failures visible and leaves the editor's draft intact", async () => {
  fetch
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({ text: "Base", base_sha256: "a".repeat(64), customized: false }),
      ),
    )
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "Could not save" }), { status: 503 }),
    );
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <BuiltinPromptEditor agentId="agent-two" />
    </QueryClientProvider>,
  );
  const input = await screen.findByRole("textbox");
  fireEvent.change(input, { target: { value: "Keep my edit" } });
  fireEvent.click(screen.getByRole("button", { name: "Save prompt" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not save");
  expect(input).toHaveValue("Keep my edit");
});

it("labels agents that build their own prompt and explains it on click", async () => {
  render(<SessionPromptField value="" onChange={vi.fn()} name="Agent Z" injectsPrompt={false} />);
  expect(screen.getByText("Not sent · builds its own prompt")).toBeInTheDocument();
  expect(screen.queryByRole("textbox")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Why startup prompts do not reach Agent Z" }));
  expect(
    await screen.findByText(/Agent Z builds its own prompt on the machine/),
  ).toBeInTheDocument();
});
