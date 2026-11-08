import { readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Compare extensionless paths: Foo.tsx and foo.ts resolve ambiguously on Windows.
export function moduleCaseConflicts(files) {
  const modules = new Map();
  for (const file of files.toSorted()) {
    if (!/\.(?:[cm]?[jt]sx?|json)$/i.test(file)) continue;
    const key = file.replace(/\.[^.]+$/, "").toLowerCase();
    const group = modules.get(key) ?? [];
    group.push(file);
    modules.set(key, group);
  }
  return [...modules.values()].filter(
    (group) => new Set(group.map((file) => file.replace(/\.[^.]+$/, ""))).size > 1,
  );
}

export function checkModuleCasing(root) {
  const files = readdirSync(root, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => path.relative(root, path.join(entry.parentPath, entry.name)));
  const conflicts = moduleCaseConflicts(files);
  if (conflicts.length) {
    throw new Error(
      `Case-insensitive module conflicts:\n${conflicts.map((group) => group.join(" / ")).join("\n")}`,
    );
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  checkModuleCasing(fileURLToPath(new URL("../src", import.meta.url)));
}
