import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { checkModuleCasing, moduleCaseConflicts } from "./check-module-casing.mjs";

test("rejects ambiguous stems across resolver extensions and directory casing", () => {
  assert.deepEqual(moduleCaseConflicts(["pages/Foo.tsx", "pages/foo.ts"]), [
    ["pages/Foo.tsx", "pages/foo.ts"],
  ]);
  assert.deepEqual(moduleCaseConflicts(["Pages/Foo.jsx", "pages/Foo.js"]), [
    ["Pages/Foo.jsx", "pages/Foo.js"],
  ]);
  assert.deepEqual(moduleCaseConflicts(["foo.mts", "Foo.json"]), [["Foo.json", "foo.mts"]]);
});

test("allows distinct helpers, directories, tests, declarations and styles", () => {
  assert.deepEqual(
    moduleCaseConflicts([
      "Foo.tsx",
      "fooData.ts",
      "foo.test.ts",
      "foo.d.ts",
      "foo.css",
      "other/foo.ts",
      "same.test.ts",
      "same.test.tsx",
    ]),
    [],
  );
});

test("scans nested source files and reports both offending paths", () => {
  const root = mkdtempSync(path.join(tmpdir(), "module-casing-"));
  try {
    mkdirSync(path.join(root, "pages"));
    writeFileSync(path.join(root, "pages/Foo.tsx"), "");
    writeFileSync(path.join(root, "pages/foo.ts"), "");
    assert.throws(() => checkModuleCasing(root), /pages[/\\]Foo.tsx.*pages[/\\]foo.ts/);
    rmSync(path.join(root, "pages/foo.ts"));
    assert.doesNotThrow(() => checkModuleCasing(root));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
