import { MOD_KEY } from "@/components/KeyboardShortcut";

import { settingsEntry } from "./types";

export const OPEN_LINKS_IN_APP_ENTRY = settingsEntry({
  id: "open-links-in-app",
  available: (availability) => availability.inAppBrowser,
  section: "browser",
  label: "Open links in the in-app browser",
  description: `Open chat links in-app, reusing matching tabs. ${MOD_KEY}+click also opens in-app; Shift+click opens your default browser.`,
  keywords: ["links", "browser", "in-app", "tabs", "click"],
});
