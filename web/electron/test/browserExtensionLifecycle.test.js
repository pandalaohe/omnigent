"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const { refreshExtensionHosts } = require("../src/browserExtensionLifecycle");

test("removal visits dormant browser partitions and clears each before reload", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "lifecycle-test-"));
  try {
    const dormant = `omnigent-agent-shared-${"a".repeat(64)}`;
    await fs.mkdir(path.join(root, "Partitions", dormant), { recursive: true });
    await fs.mkdir(path.join(root, "Partitions", "unrelated"));
    const calls = [],
      hosts = new Map([
        [
          "active",
          {
            clearData: async (record) => calls.push(["clear-active", record.runtimeId]),
            load: async () => calls.push(["load-active"]),
          },
        ],
      ]);
    const createHost = (partition) => {
      assert.equal(partition, `persist:${dormant}`);
      hosts.set(partition, {
        clearData: async (record) => calls.push(["clear-dormant", record.runtimeId]),
        load: async () => calls.push(["load-dormant"]),
      });
    };
    await refreshExtensionHosts({
      hosts,
      userData: root,
      createHost,
      remove: { runtimeId: "b".repeat(32) },
    });
    assert.deepEqual(
      calls.map((call) => call[0]),
      ["clear-active", "clear-dormant", "load-active", "load-dormant"],
    );
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("a failed partition cannot release the operation while another partition is still running", async () => {
  let release,
    settled = false,
    reloads = 0;
  const slow = new Promise((resolve) => {
    release = resolve;
  });
  const hosts = new Map([
    [
      "failed",
      {
        unload: async () => {
          throw Error("Synthetic failure");
        },
        reloadViews: () => {
          reloads++;
        },
      },
    ],
    [
      "slow",
      {
        unload: () => slow,
        reloadViews: () => {
          reloads++;
        },
      },
    ],
  ]);
  const operation = refreshExtensionHosts({ hosts });
  const rejected = assert.rejects(operation, /Synthetic failure/).then(() => {
    settled = true;
  });
  await new Promise((resolve) => {
    setImmediate(resolve);
  });
  assert.equal(settled, false);
  release();
  await rejected;
  assert.equal(reloads, 2);
});
