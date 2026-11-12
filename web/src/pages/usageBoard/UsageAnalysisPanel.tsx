import type { ReactNode } from "react";

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
      <strong>Unknown</strong>
      <small>{reason}</small>
    </div>
  );
}
