import { build } from "esbuild";
import { copyFile, mkdir, readFile, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = fileURLToPath(new URL("../", import.meta.url));
const output = path.join(root, "dist");
const manifest = JSON.parse(await readFile(path.join(root, "manifest.json"), "utf8"));
if (manifest.manifest_version !== 3 || !manifest.key) throw new Error("Missing MV3 identity");
await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
await build({
  absWorkingDir: root,
  entryPoints: {
    background: "src/background.ts",
    picker: "src/picker/index.ts",
    sidepanel: "src/sidepanel/index.ts",
  },
  bundle: true,
  outdir: output,
  format: "iife",
  target: "chrome116",
  legalComments: "eof",
  charset: "utf8",
});
for (const file of ["manifest.json", "sidepanel.html", "NOTICE"]) {
  await copyFile(path.join(root, file), path.join(output, file));
}
