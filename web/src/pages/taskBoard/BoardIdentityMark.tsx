import Claude from "@lobehub/icons/es/Claude/components/Mono";
import Codex from "@lobehub/icons/es/Codex/components/Mono";
import Antigravity from "@lobehub/icons/es/Antigravity/components/Mono";

const colours = [
  "#ee352e",
  "#0091c8",
  "#00933c",
  "#b933ad",
  "#ff6319",
  "#2f7fd4",
  "#d4972a",
  "#00a1a7",
  "#7c5cdb",
  "#df3c85",
  "#6cbe45",
  "#996633",
];
export function BoardIdentityMark({
  id,
  label,
  product,
  size = 16,
}: {
  id: string;
  label: string;
  product?: string | null;
  size?: number;
}) {
  let hash = 0;
  for (let index = 0; index < id.length; index++) hash = (hash * 31 + id.charCodeAt(index)) >>> 0;
  const Icon =
    product === "claude"
      ? Claude
      : product === "codex"
        ? Codex
        : product === "antigravity"
          ? Antigravity
          : null;
  return (
    <span
      className={`tb-identity-mark${Icon ? ` tb-brand-${product}` : ""}`}
      aria-hidden="true"
      style={{
        width: size,
        height: size,
        borderRadius: size * 0.28,
        background: Icon ? undefined : colours[hash % colours.length],
        fontSize: Math.round(size * 0.58),
      }}
    >
      {Icon ? <Icon size={Math.round(size * 0.62)} /> : label.slice(0, 1).toUpperCase()}
    </span>
  );
}

export function BoardProjectBullet({ label, index }: { label: string; index: number }) {
  return (
    <span
      className="tb-project-bullet"
      aria-hidden="true"
      style={{ background: colours[index % colours.length] }}
    >
      {label.slice(0, 1).toUpperCase()}
    </span>
  );
}
