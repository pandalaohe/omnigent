import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

describe("MV3 package identity and authority", () => {
  const manifest = JSON.parse(readFileSync(new URL("../manifest.json", import.meta.url), "utf8"));
  it("pins a public key and stable unpacked extension ID across OS builds", () => {
    const id = createHash("sha256")
      .update(Buffer.from(manifest.key, "base64"))
      .digest("hex")
      .slice(0, 32)
      .replace(/[0-9a-f]/g, (digit) => String.fromCharCode(97 + parseInt(digit, 16)));
    expect(id).toBe("kijpmhjibehfipeillfolgdodncokjol");
    expect(manifest.manifest_version).toBe(3);
    expect(manifest.minimum_chrome_version).toBe("116");
  });
  it("has no installed site access, external/page bridge, remote code or registered content scripts", () => {
    expect(manifest.permissions).toEqual(["activeTab", "scripting", "sidePanel", "storage"]);
    expect(manifest.optional_host_permissions).toEqual(["https://*/*"]);
    for (const key of [
      "host_permissions",
      "externally_connectable",
      "web_accessible_resources",
      "content_scripts",
    ])
      expect(manifest).not.toHaveProperty(key);
    expect(manifest.content_security_policy.extension_pages).toBe(
      "script-src 'self'; object-src 'none'",
    );
    expect(manifest.commands._execute_action.suggested_key).toEqual({
      default: "Ctrl+Shift+Y",
      mac: "Command+Shift+Y",
    });
  });
});
