export interface TerminalAccessRequest {
  owner: string;
  terminalId: string;
  instanceId: string;
  hostId: string;
}

export function isTerminalAccessPolicyName(name: string): boolean {
  return name === "ask_on_terminal_agent_access" || name === "__ask_on_terminal_agent_access";
}

export function parseTerminalAccess(value: unknown): TerminalAccessRequest | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const params = value as Record<string, unknown>;
  const owner = params.terminal_access_owner;
  const terminalId = params.terminal_access_terminal_id;
  const instanceId = params.terminal_access_instance_id;
  const hostId = params.terminal_access_host_id;
  if (
    typeof owner !== "string" ||
    !owner ||
    typeof terminalId !== "string" ||
    !terminalId ||
    typeof instanceId !== "string" ||
    !instanceId ||
    typeof hostId !== "string" ||
    !hostId
  )
    return null;
  return { owner, terminalId, instanceId, hostId };
}
