import { TerminalBarEditor } from "@/components/settings/TerminalBarEditor";
import { useMobileAssistantPreferences } from "@/lib/mobileAssistantPreferences";

export function MobileAssistantSettings() {
  const preferences = useMobileAssistantPreferences();
  return <TerminalBarEditor preferences={preferences} />;
}
