const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

function sourceInfo(tool) {
  const source = tool && tool.sourceInfo;
  if (!source || typeof source !== "object") return null;
  return Object.fromEntries(["path", "source", "scope", "origin", "baseDir"]
    .filter((key) => typeof source[key] === "string").map((key) => [key, source[key]]));
}

function capture(pi, installation) {
  const tools = typeof pi.getAllTools === "function" ? pi.getAllTools() : [];
  return { ...installation, tools: tools.map((tool) => {
    const source = sourceInfo(tool);
    const record = { name: tool.name, sourceInfo: source };
    if (!source || source.scope !== "user" || !path.isAbsolute(source.path || "") ||
        !path.isAbsolute(source.baseDir || "")) return record;
    try {
      const entry = fs.realpathSync(source.path);
      if (!fs.statSync(entry).isFile()) return record;
      let root = path.dirname(entry);
      while (!fs.existsSync(path.join(root, "package.json")) && path.dirname(root) !== root)
        root = path.dirname(root);
      if (path.dirname(root) === root) root = path.dirname(entry);
      return { ...record, real_path: entry, implementation_root: root,
        real_base_dir: fs.realpathSync(source.baseDir),
        source_sha256: crypto.createHash("sha256").update(fs.readFileSync(entry)).digest("hex") };
    } catch (_err) { return record; }
  }) };
}

function matches(pi, registry, name) {
  if (!registry || typeof pi.getAllTools !== "function") return false;
  const saved = registry.tools.find((tool) => tool.name === name);
  const current = pi.getAllTools().find((tool) => tool.name === name);
  return !!saved && !!current && JSON.stringify(saved.sourceInfo) === JSON.stringify(sourceInfo(current));
}

function isBridge(registry, name, bridgePath) {
  const source = registry && registry.tools.find((tool) => tool.name === name)?.sourceInfo;
  return !!source && [bridgePath, "<inline:omnigent-permission>"].includes(source.path);
}

module.exports = { capture, matches, isBridge };
