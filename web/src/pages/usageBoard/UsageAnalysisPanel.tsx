import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

export function AnalysisPanel({
  title,
  caption,
  children,
}: {
  title: string;
  caption?: string;
  children: ReactNode;
}) {
  return (
    <section className="ua-panel" aria-label={title}>
      <h2>
        {title}
        <span>{caption}</span>
      </h2>
      {children}
    </section>
  );
}
export function AnalysisUnknown({ reason = "Not available yet" }: { reason?: string }) {
  return (
    <div className="ua-unknown">
      <strong data-unknown="">Unknown</strong>
      <small>{reason}</small>
    </div>
  );
}

const SectionOpenness = createContext<{
  open: Record<string, boolean>;
  setOpen: (id: string, value: boolean) => void;
} | null>(null);

export function AnalysisSectionState({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const sectionState = useMemo(
    () => ({
      open,
      setOpen: (id: string, value: boolean) =>
        setOpen((current) => (!!current[id] === value ? current : { ...current, [id]: value })),
    }),
    [open],
  );
  return <SectionOpenness.Provider value={sectionState}>{children}</SectionOpenness.Provider>;
}

export function AnalysisSection({
  id,
  title,
  folded,
  children,
}: {
  id: string;
  title: string;
  folded?: boolean;
  children: ReactNode;
}) {
  const state = useContext(SectionOpenness);
  const open = state?.open[id] ?? false;
  if (folded)
    return (
      <details
        id={`analysis-${id}`}
        className="ua-panel ua-v2-fold"
        open={open}
        onToggle={(event) => state?.setOpen(id, event.currentTarget.open)}
      >
        <summary>{title}</summary>
        {open ? children : null}
      </details>
    );
  return (
    <section id={`analysis-${id}`} className="ua-panel ua-v2-section" aria-label={title}>
      <h2>{title}</h2>
      {children}
    </section>
  );
}
