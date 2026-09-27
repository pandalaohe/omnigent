// Entry for the visitor shell the server serves for an admitted `g` link.

import { readVisitConfig } from "./visitConfig";
import { mountVisitShell } from "./visitShell";

const config = readVisitConfig(document);
if (config !== null) {
  mountVisitShell(document.body, config);
}
