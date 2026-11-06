import ts from "typescript";
import { expect, it } from "vitest";

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
