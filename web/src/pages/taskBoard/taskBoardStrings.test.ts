import { boardDefaults } from "./taskBoardPreferences";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { cleanup, render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement } from "react";
import { MemoryRouter } from "react-router-dom";
import ts from "typescript";
import { build } from "vite";
import { afterEach, expect, it, vi } from "vitest";
import english from "./locales/en.json";
import { taskBoardFixture } from "./taskBoardFixtures";
import { TaskBoardSettings } from "./TaskBoardSettings";
import { TaskBoardSources } from "./TaskBoardSources";
import type * as TaskBoardTextModule from "./taskBoardText";

const activeBundle = vi.hoisted(() => ({
  current: null as typeof TaskBoardTextModule | null,
}));

vi.mock("./taskBoardText", async (importOriginal) => {
  const actual = await importOriginal<typeof TaskBoardTextModule>();
  return {
    ...actual,
    t: (...args: Parameters<typeof actual.t>) => (activeBundle.current ?? actual).t(...args),
    formatDate: (...args: Parameters<typeof actual.formatDate>) =>
      (activeBundle.current ?? actual).formatDate(...args),
    formatNumber: (...args: Parameters<typeof actual.formatNumber>) =>
      (activeBundle.current ?? actual).formatNumber(...args),
  };
});

afterEach(() => {
  activeBundle.current = null;
  cleanup();
  vi.restoreAllMocks();
});

async function buildProductionTextBundle(overridePack?: Record<string, unknown>) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-task-board-pack-"));
  try {
    fs.mkdirSync(path.join(dir, "locales"), { recursive: true });
    fs.copyFileSync(
      path.join(import.meta.dirname, "taskBoardText.ts"),
      path.join(dir, "taskBoardText.ts"),
    );
    fs.copyFileSync(
      path.join(import.meta.dirname, "locales", "en.json"),
      path.join(dir, "locales", "en.json"),
    );
    if (overridePack) {
      fs.writeFileSync(
        path.join(dir, "locales", "override.json"),
        JSON.stringify(overridePack, null, 2),
      );
    }
    const output = await build({
      root: dir,
      logLevel: "silent",
      build: {
        lib: {
          entry: path.join(dir, "taskBoardText.ts"),
          formats: ["es"],
          fileName: "taskBoardText",
        },
        write: false,
      },
    });
    const firstResult = Array.isArray(output) ? output[0] : output;
    if (!firstResult || !("output" in firstResult)) {
      throw new Error("Expected RollupOutput from production bundle build");
    }
    const chunk = firstResult.output.find(
      (item: { type: string; isEntry?: boolean; code?: string }) =>
        item.type === "chunk" && item.isEntry,
    );
    if (!chunk || chunk.type !== "chunk" || typeof chunk.code !== "string") {
      throw new Error("Expected entry chunk from production bundle build");
    }
    const moduleUrl = `data:text/javascript;base64,${Buffer.from(chunk.code).toString("base64")}`;
    return (await import(/* @vite-ignore */ moduleUrl)) as typeof TaskBoardTextModule;
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

function visibleLiterals(source: string): string[] {
  const file = ts.createSourceFile(
    "component.tsx",
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const literals: string[] = [];
  const visit = (node: ts.Node) => {
    if (ts.isJsxText(node) && node.getText(file).trim()) literals.push(node.getText(file).trim());
    if (
      ts.isJsxAttribute(node) &&
      ["aria-label", "title", "placeholder", "alt"].includes(node.name.getText(file)) &&
      node.initializer &&
      ts.isStringLiteral(node.initializer)
    )
      literals.push(node.initializer.text);
    ts.forEachChild(node, visit);
  };
  visit(file);
  return literals;
}

it("requires board components to take every visible label from the catalogue", () => {
  const components = import.meta.glob<string>(["./*.tsx", "!./*.test.tsx"], {
    query: "?raw",
    import: "default",
    eager: true,
  });
  for (const [file, source] of Object.entries(components)) {
    expect(visibleLiterals(source), file).toEqual([]);
  }
  expect(visibleLiterals('<button aria-label="Retry">Try again</button>')).toEqual([
    "Retry",
    "Try again",
  ]);
});

it("builds the production bundle without and with a complete override.json and checks the rendered board", async () => {
  const englishStrings = english as unknown as Record<string, string>;
  const keys = Object.keys(english).filter((key) => key !== "_meta");
  const staticEnglish = Object.entries(english)
    .filter(
      ([key, value]) =>
        key !== "_meta" && typeof value === "string" && !value.includes("{{") && value.length > 4,
    )
    .map(([, value]) => value as string);
  const completeOverride: Record<string, unknown> = {
    _meta: { locale: "zh-CN", fallback: "en", direction: "ltr" },
    ...Object.fromEntries(
      keys.map((key) => [
        key,
        `[PACK:${key}]` + (englishStrings[key].match(/\{\{\w+\}\}/g)?.join(" ") ?? ""),
      ]),
    ),
  };

  const renderBoardSlices = (bundle: typeof TaskBoardTextModule) => {
    activeBundle.current = bundle;
    const source = {
      ...taskBoardFixture().sources[0],
      counts: { open: 2, closed: 3, gone: 1 },
      issues: [{ code: "missing_property", path: "entries[0].uid" }],
    };
    return render(
      createElement(
        QueryClientProvider,
        { client: new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } }) },
        createElement(
          MemoryRouter,
          null,
          createElement(TaskBoardSettings),
          createElement(TaskBoardSources, {
            sources: [source],
            preferences: {
              ...boardDefaults,
              view: "settings",
              mode: "order",
              focus: [],
              aliases: {},
              hiddenSources: [],
              pinnedQuota: [],
            },
            update: () => true,
          }),
        ),
      ),
    );
  };

  const defaultBundle = await buildProductionTextBundle();
  const firstView = renderBoardSlices(defaultBundle);
  expect(firstView.container.textContent).toContain("Connect this source");
  expect(firstView.container.textContent).toContain("Show Task board");
  firstView.unmount();

  const overrideBundle = await buildProductionTextBundle(completeOverride);
  const secondView = renderBoardSlices(overrideBundle);
  expect(secondView.container.textContent).toContain("[PACK:sources.connect]");
  expect(secondView.container.textContent).toContain("[PACK:settings.show]");
  for (const label of staticEnglish) {
    expect(secondView.container.textContent).not.toContain(label);
  }
  for (const key of keys) {
    const baseKey = key.replace(/_(one|other)$/, "");
    expect(overrideBundle.t(baseKey, { count: 2, name: "X", path: "uid" })).toContain("[PACK:");
  }

  await expect(buildProductionTextBundle({ "board.staleKey": "obsolete" })).rejects.toThrow(
    "Unknown task board translation key: board.staleKey",
  );
});
