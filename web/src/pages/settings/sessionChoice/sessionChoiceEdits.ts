import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
import type { UsageAccountView } from "@/pages/usageBoard/usageBoardData";
import { canonicalNativeHarness } from "@/lib/nativeHarnessModes";
import {
  EMPTY_CHOICE,
  type ChoiceLink,
  type ChoiceRow,
  type SessionChoiceDocument,
} from "./sessionChoiceData";

export type ChoiceAgent = Pick<AvailableAgent, "id" | "name" | "display_name" | "harness"> & {
  product?: string | null;
};

export function choiceId(label: string): string {
  return label
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^\p{L}\p{N}_-]/gu, "")
    .slice(0, 32);
}

export function renameWorkType(
  document: SessionChoiceDocument,
  oldId: string,
  label: string,
): SessionChoiceDocument {
  const id = choiceId(label);
  if (!id) throw new Error("Enter a work type name.");
  if (document.workTypes.some((w) => w.id !== oldId && (w.id === id || w.aliases?.includes(id))))
    throw new Error("That work type name is already in use.");
  const next = structuredClone(document);
  next.workTypes = next.workTypes.map((w) =>
    w.id !== oldId
      ? w
      : {
          ...w,
          id,
          label: label.trim(),
          aliases: [...new Set([...(w.aliases ?? []), ...(id === oldId ? [] : [oldId])])].filter(
            (alias) => alias !== id,
          ),
        },
  );
  if ((next.defaultWorkType ?? document.workTypes[0]?.id) === oldId) next.defaultWorkType = id;
  for (const types of Object.values(next.hosts)) {
    if (oldId !== id && types[oldId]) {
      types[id] = types[oldId];
      Reflect.deleteProperty(types, oldId);
    }
  }
  for (const limit of Object.values(next.limits))
    limit.reserveExempt = limit.reserveExempt?.map((w) => (w === oldId ? id : w));
  return next;
}

export function removeWorkType(document: SessionChoiceDocument, id: string): SessionChoiceDocument {
  if (
    document.workTypes.length > 1 &&
    (document.defaultWorkType ?? document.workTypes[0]?.id) === id
  )
    throw new Error("Choose another default work type first.");
  const next = structuredClone(document);
  next.workTypes = next.workTypes.filter((w) => w.id !== id);
  for (const types of Object.values(next.hosts)) Reflect.deleteProperty(types, id);
  for (const limit of Object.values(next.limits))
    limit.reserveExempt = limit.reserveExempt?.filter((w) => w !== id);
  if (!next.workTypes.length) {
    next.enabled = false;
    next.defaultWorkType = null;
    next.hosts = {};
  }
  return next;
}

export function applyPreset(document: SessionChoiceDocument, presetId: string) {
  const next = structuredClone(document);
  const preset = next.presets.find((p) => p.id === presetId);
  let updated = 0;
  let adjusted = 0;
  if (preset)
    for (const types of Object.values(next.hosts))
      for (const tiers of Object.values(types))
        for (const tier of tiers)
          for (const row of tier) {
            if (row.preset !== presetId) continue;
            if (row.adjusted) {
              adjusted++;
              continue;
            }
            row.links = structuredClone(preset.links);
            row.name = preset.name;
            updated++;
          }
  return { document: next, updated, adjusted };
}

export function removePreset(document: SessionChoiceDocument, id: string) {
  const next = structuredClone(document);
  const name = next.presets.find((p) => p.id === id)?.name;
  next.presets = next.presets.filter((p) => p.id !== id);
  next.abilityOrder = next.abilityOrder
    .map((level) => level.filter((p) => p !== id))
    .filter((level) => level.length);
  for (const types of Object.values(next.hosts))
    for (const tiers of Object.values(types))
      for (const tier of tiers)
        for (const row of tier)
          if (row.preset === id) {
            row.adjusted = true;
            row.name ??= name;
          }
  return next;
}

export function agentReady(agent: ChoiceAgent, host: Host | undefined) {
  if (
    !agent.harness ||
    !host?.configured_harnesses ||
    !Object.keys(host.configured_harnesses).length
  )
    return true;
  const state =
    host.configured_harnesses[canonicalNativeHarness(agent.harness.replaceAll("_", "-"))];
  return state === true || state === "needs-auth";
}

export function guessMeter(
  link: ChoiceLink,
  agents: ChoiceAgent[],
  meters: UsageAccountView[],
): string | null {
  if (link.model?.includes("/") || link.model?.includes(":")) return null;
  const product = agents.find((a) => a.id === link.agent)?.product;
  return meters.find((m) => m.product === product && product != null)?.id ?? null;
}

export function copyStrategy(
  document: SessionChoiceDocument,
  sourceHost: string,
  sourceWork: string,
  targetHost: Host,
  targetWork: string,
  agents: ChoiceAgent[],
  models: Record<string, string[]> = {},
) {
  const next = structuredClone(document);
  const changes: string[] = [];
  const source = document.hosts[sourceHost] ?? {};
  const entries =
    sourceWork === "*" ? Object.entries(source) : [[targetWork, source[sourceWork] ?? []] as const];
  if (sourceWork === "*") next.hosts[targetHost.host_id] = {};
  else next.hosts[targetHost.host_id] ??= {};
  for (const [work, tiers] of entries) {
    next.hosts[targetHost.host_id][work] = tiers.map((tier) =>
      tier.flatMap((original) => {
        const row = structuredClone(original);
        if (sourceHost !== targetHost.host_id)
          row.links = row.links.flatMap((link) => {
            const agent = agents.find((a) => a.id === link.agent);
            let replacement = agent;
            if (!agent || !agentReady(agent, targetHost)) {
              replacement = agent?.product
                ? agents.find((a) => a.product === agent.product && agentReady(a, targetHost))
                : undefined;
              row.adjusted = true;
              if (!replacement) {
                changes.push(`Dropped ${agent?.display_name ?? link.agent}`);
                return [];
              }
              changes.push(`${agent?.display_name ?? link.agent} → ${replacement.display_name}`);
            }
            const copied = { ...link, agent: replacement!.id };
            if (copied.model && !models[copied.agent]?.includes(copied.model))
              changes.push(`${copied.model}: not in ${targetHost.name}'s cached list; kept`);
            return [copied];
          });
        return row.links.length ? [row] : [];
      }),
    );
  }
  return { document: next, changes: [...new Set(changes)] };
}

export function addToOtherTypes(
  document: SessionChoiceDocument,
  host: string,
  presetId: string,
  tierIndex: number,
) {
  const next = structuredClone(document);
  const preset = next.presets.find((p) => p.id === presetId);
  if (!preset) return next;
  next.hosts[host] ??= {};
  for (const work of next.workTypes) {
    const tiers = (next.hosts[host][work.id] ??= []);
    if (tiers.some((tier) => tier.some((row) => row.preset === presetId))) continue;
    while (tiers.length <= tierIndex) tiers.push([]);
    tiers[tierIndex].push({
      preset: presetId,
      name: preset.name,
      links: structuredClone(preset.links),
    });
  }
  return next;
}

export function starter(
  host: Host,
  agents: ChoiceAgent[],
  meters: UsageAccountView[],
  simple: boolean,
): SessionChoiceDocument {
  const next = structuredClone(EMPTY_CHOICE);
  next.workTypes = simple
    ? [{ id: "work", label: "Work" }]
    : [
        {
          id: "design",
          label: "Design",
          definition: "Decide an approach and explain its trade-offs.",
        },
        { id: "big-design", label: "Big design" },
        { id: "implementation", label: "Implementation" },
        { id: "review", label: "Review", ifFull: "ask" },
        {
          id: "mechanical",
          label: "Mechanical",
          definition: "Make a bounded change with a settled method.",
        },
        { id: "image", label: "Image" },
      ];
  next.defaultWorkType = simple ? "work" : "implementation";
  next.presets = agents
    .filter((agent) => agentReady(agent, host))
    .map((agent, index) => {
      const link: ChoiceLink = { agent: agent.id };
      link.meter = guessMeter(link, agents, meters);
      return { id: `preset-${index + 1}`, name: agent.display_name, links: [link] };
    });
  next.hosts[host.host_id] = Object.fromEntries(
    next.workTypes.map((w) => [
      w.id,
      [next.presets.map((p): ChoiceRow => ({ preset: p.id, links: structuredClone(p.links) }))],
    ]),
  );
  return next;
}

export function weightOf(row: ChoiceRow, position: number, size: number) {
  return row.weight ?? Math.min(9, size - position);
}

export const CHOICE_LINE =
  "Session choice is on: open sessions with sys_session_create(work_type=<ids>); the app picks agent, model and effort and returns why. Do not read quota to choose. Name agent_id only if the user did (user_named=true) or with choice_reason; otherwise it is ignored.";

export function choiceTokenEstimate(document: SessionChoiceDocument) {
  const definitions = document.workTypes
    .filter((w) => w.definition?.trim())
    .map((w) => `${w.id}: ${w.definition}`)
    .join("; ");
  const text = `${CHOICE_LINE.replace("<ids>", `<${document.workTypes.map((w) => w.id).join("|")}>`)} ${definitions}`;
  const cjk =
    text.match(/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/gu)
      ?.length ?? 0;
  return Math.ceil((text.length - cjk) / 4 + cjk);
}
