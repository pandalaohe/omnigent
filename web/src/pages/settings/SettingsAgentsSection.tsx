import { useEffect, useState } from "react";
import { CallingDefaultsSection } from "@/components/CallingDefaultsSection";
import { useSettingsRoute } from "@/shell/settingsNav";
import { SettingsCustomAgentsSection } from "./SettingsCustomAgentsSection";
import { SettingsHarnessesSection } from "./SettingsHarnessesSection";

export function SettingsAgentsSection() {
  const { harness, agentId } = useSettingsRoute();
  const viewingDetail = Boolean(agentId || harness);
  const [overviewVisited, setOverviewVisited] = useState(!viewingDetail);
  useEffect(() => {
    if (!viewingDetail) setOverviewVisited(true);
  }, [viewingDetail]);
  return (
    <div className="mx-auto flex w-full min-w-0 max-w-[720px] flex-col gap-8">
      {(!viewingDetail || overviewVisited) && (
        <section
          id="agent-settings-table"
          hidden={viewingDetail}
          className="min-w-0 space-y-5"
          aria-label="Agents"
        >
          <h1 className="settings-page-title text-2xl font-medium">Agents</h1>
          <CallingDefaultsSection />
        </section>
      )}
      {harness ? <SettingsHarnessesSection /> : <SettingsCustomAgentsSection />}
      {!viewingDetail && <SettingsHarnessesSection />}
    </div>
  );
}
