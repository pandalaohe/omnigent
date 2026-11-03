import { useEffect, useState } from "react";
import { FolderIcon } from "lucide-react";

import {
  type TablerIconNode,
  type TablerIconSet,
  loadTablerIcons,
  tablerIconsIfLoaded,
} from "@/lib/tablerIconData";
import { cn } from "@/lib/utils";

export const TABLER_PREFIX = "tabler:";

export function parseProjectIcon(
  value: string | null | undefined,
): { kind: "none" } | { kind: "emoji"; text: string } | { kind: "tabler"; name: string } {
  if (!value) return { kind: "none" };
  if (value.startsWith(TABLER_PREFIX)) {
    return { kind: "tabler", name: value.slice(TABLER_PREFIX.length) };
  }
  return { kind: "emoji", text: value };
}

export function tablerValue(name: string): string {
  return `${TABLER_PREFIX}${name}`;
}

export function useTablerIcons(): TablerIconSet | "error" | null {
  const [set, setSet] = useState<TablerIconSet | "error" | null>(tablerIconsIfLoaded);
  useEffect(() => {
    if (set !== null) return undefined;
    let live = true;
    loadTablerIcons().then(
      (loaded) => live && setSet(loaded),
      () => live && setSet("error"),
    );
    return () => {
      live = false;
    };
  }, [set]);
  return set;
}

export function TablerSvg({ node, className }: { node: TablerIconNode; className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className}
    >
      {node.map(([, attrs], index) => (
        <path
          // The icon's paths are static, so their positions are stable keys.
          // oxlint-disable-next-line react/no-array-index-key
          key={index}
          d={attrs.d}
          fill={attrs.fill}
          stroke={attrs.stroke}
          opacity={attrs.opacity}
        />
      ))}
    </svg>
  );
}

/** A Tabler row holds its footprint while loading, then draws or falls back. */
export function TablerRowIcon({ name, className }: { name: string; className?: string }) {
  const set = useTablerIcons();
  const node = set && set !== "error" ? set[name] : undefined;
  if (set === "error" || (set && !node)) {
    return (
      <FolderIcon
        aria-hidden="true"
        className={cn("size-3.5 shrink-0 text-muted-foreground", className)}
      />
    );
  }
  return (
    <span
      aria-hidden="true"
      className={cn(
        "inline-flex size-3.5 shrink-0 items-center justify-center text-muted-foreground",
        className,
      )}
      data-testid="project-icon"
      data-tabler-icon={name}
    >
      {node ? <TablerSvg node={node} className="size-full" /> : null}
    </span>
  );
}

/** A project tile glyph; its caller's font size sizes Emoji and Tabler alike. */
export function ProjectIconGlyph({
  icon,
  className,
}: {
  icon?: string | null;
  className?: string;
}) {
  const parsed = parseProjectIcon(icon);
  if (parsed.kind === "emoji") return <span className={className}>{parsed.text}</span>;
  if (parsed.kind === "none") {
    return <FolderIcon aria-hidden="true" className={className ?? "size-[1em]"} />;
  }
  return <TablerGlyph name={parsed.name} className={className} />;
}

function TablerGlyph({ name, className }: { name: string; className?: string }) {
  const set = useTablerIcons();
  const node = set && set !== "error" ? set[name] : undefined;
  if (set === "error" || (set && !node)) {
    return <FolderIcon aria-hidden="true" className={className ?? "size-[1em]"} />;
  }
  return node ? (
    <TablerSvg node={node} className={className ?? "size-[1em]"} />
  ) : (
    <span aria-hidden="true" className={cn("inline-block size-[1em]", className)} />
  );
}
