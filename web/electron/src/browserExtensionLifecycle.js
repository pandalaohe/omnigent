"use strict";
const fs = require("node:fs/promises");
const path = require("node:path");

async function refreshExtensionHosts({ hosts, userData, createHost, remove }) {
  if (remove) {
    let saved = [];
    try {
      saved = await fs.readdir(path.join(userData, "Partitions"));
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    for (const name of saved)
      if (/^omnigent-agent-shared-[0-9a-f]{64}$/.test(name)) createHost(`persist:${name}`);
  }
  const settle = async (operation) => {
    const results = await Promise.allSettled([...hosts.values()].map(operation));
    const failed = results.find((result) => result.status === "rejected");
    if (failed) throw failed.reason;
  };
  try {
    await settle((host) => (remove ? host.clearData(remove) : host.unload()));
    await settle((host) => host.load());
  } finally {
    for (const host of hosts.values()) host.reloadViews?.();
  }
}
module.exports = { refreshExtensionHosts };
