import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { HarnessAccount, Host } from "@/hooks/useHosts";
import type { InstructionLayers, InstructionPreview } from "@/hooks/useInstructionLayers";
import { AgentContextTable, AgentPromptCard } from "./AgentContextTable";

const mocks = vi.hoisted(() => ({
  layers: undefined as InstructionLayers | undefined,
  preview: undefined as InstructionPreview | undefined,
  error: false,
}));
vi.mock("@/hooks/useInstructionLayers", () => ({
  useInstructionLayers: () => ({
    data: mocks.layers,
    isPending: false,
    isError: mocks.error,
    refetch: vi.fn(),
  }),
  useInstructionPreview: () => ({ data: mocks.preview, isPending: false, isError: false }),
}));

const host: Host = {
  host_id: "host & one",
  name: "Desktop",
  owner: "local",
  status: "online",
  platform: "darwin",
  configured_harnesses: { "codex-native": true, codex: true },
  harness_accounts: {
    codex: {
      kind: "providers",
      providers: [{ id: "example", label: "Example provider", auth_type: "api_key" }],
    },
  },
};
const props = {
  hosts: [host],
  harnesses: ["codex-native", "codex"],
  names: { "codex-native": "Codex", codex: "Codex SDK" },
  agentIds: {},
  capabilities: {},
};
const layers: InstructionLayers = {
  data: [],
  agents: [{ key: "codex", display_name: "Codex", harnesses: ["codex-native", "codex"] }],
  order: [],
  route_sizes: [{ host_id: host.host_id, agent_key: "codex", total_chars: 123 }],
  size_reminder_chars: 1000,
};
afterEach(() => {
  cleanup();
  mocks.layers = undefined;
  mocks.preview = undefined;
  mocks.error = false;
});

describe("agent context lenses", () => {
  it.each([
    ["none", "Not signed in"],
    ["unknown", "Not reported"],
    ["subscription", "Personal subscription"],
    ["chatgpt", "Personal subscription"],
    ["api_key", "API key"],
  ] as const)("describes the %s account state accurately", (kind, label) => {
    render(
      <MemoryRouter>
        <AgentContextTable
          {...props}
          field="provider"
          hosts={[{ ...host, harness_accounts: { codex: { kind } as HarnessAccount } }]}
        />
      </MemoryRouter>,
    );
    expect(screen.getAllByText(label)).toHaveLength(2);
    if (kind === "none" || kind === "unknown")
      expect(screen.queryByText("Personal subscription")).toBeNull();
  });

  it("reads the same Claude account for Native and SDK through the product family", () => {
    render(
      <MemoryRouter>
        <AgentContextTable
          {...props}
          field="provider"
          harnesses={["claude-native", "claude-sdk"]}
          names={{ "claude-native": "Claude Code", "claude-sdk": "Claude SDK" }}
          hosts={[
            {
              ...host,
              configured_harnesses: { "claude-native": true, "claude-sdk": true },
              harness_accounts: { claude: { kind: "api_key" } },
            },
          ]}
        />
      </MemoryRouter>,
    );
    expect(screen.getAllByText("API key")).toHaveLength(2);
    expect(screen.queryByText("Not reported")).toBeNull();
  });

  it("shows reported providers as text and leaves selection and editing out", () => {
    render(
      <MemoryRouter>
        <AgentContextTable {...props} field="provider" />
      </MemoryRouter>,
    );
    expect(screen.getAllByText("Example provider")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Select cells" })).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.getByRole("link", { name: "Codex details" })).toHaveAttribute(
      "href",
      "/settings/agents/codex-native?host=host%20%26%20one",
    );
    expect(screen.getByText("Codex SDK")).toHaveClass("sr-only");
  });

  it("opens the same host and agent in Startup prompts with the composed size", () => {
    mocks.layers = layers;
    render(
      <MemoryRouter>
        <AgentContextTable {...props} field="prompt" />
      </MemoryRouter>,
    );
    for (const link of screen.getAllByRole("link", { name: "123 chars" })) {
      expect(link).toHaveAttribute(
        "href",
        "/settings/instructions?view=route&agent=codex&host=host+%26+one",
      );
    }
    expect(screen.queryByRole("button", { name: /^Edit/ })).toBeNull();
  });

  it("keeps absent Windows cells as text without a control", () => {
    render(
      <MemoryRouter>
        <AgentContextTable {...props} field="provider" hosts={[{ ...host, platform: "win32" }]} />
      </MemoryRouter>,
    );
    const cell = screen.getByText("Not on Windows");
    expect(cell.closest("button")).toBeNull();
  });

  it("keeps a standalone SDK row readable when its native product is unavailable", () => {
    render(
      <MemoryRouter>
        <AgentContextTable {...props} field="provider" harnesses={["codex"]} />
      </MemoryRouter>,
    );
    expect(screen.getByText("Codex SDK")).not.toHaveClass("sr-only");
    expect(screen.getByRole("link", { name: "Codex SDK details" })).toBeInTheDocument();
  });

  it("shows composed prompt layers without offering an editor on the detail page", () => {
    mocks.layers = layers;
    mocks.preview = {
      subject: { name: "Codex", harness: "codex-native" },
      route: { host_id: host.host_id, agent_key: "codex", child: false, peer: true, spawn: true },
      parts: [
        {
          kind: "shared",
          scope: "shared",
          text: "Example shared instructions",
          chars: 27,
          sent: true,
          readonly: false,
        },
      ],
      text: "Example shared instructions",
      total_chars: 27,
      framework_unavailable: false,
    };
    render(
      <MemoryRouter>
        <AgentPromptCard harness="codex-native" host={host} />
      </MemoryRouter>,
    );
    const prompt = screen.getByRole("region", { name: "Prompt" });
    expect(within(prompt).getByText("shared · 27 chars")).toBeInTheDocument();
    expect(
      within(prompt).getByRole("link", { name: "Edit in Startup prompts" }),
    ).toBeInTheDocument();
    expect(within(prompt).queryByRole("textbox")).toBeNull();
  });
});
