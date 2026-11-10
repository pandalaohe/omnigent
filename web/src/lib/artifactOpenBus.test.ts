import { expect, it, vi } from "vitest";
import {
  clearArtifactOpenRequest,
  emitArtifactOpenRequest,
  onArtifactOpenRequest,
} from "./artifactOpenBus";

it("a cleared session never consumes its old artifact request or clears another session", () => {
  const event = {
    type: "artifact_open_request" as const,
    path: "old.html",
    base: "workspace" as const,
  };
  emitArtifactOpenRequest(event, "cleared-session");
  emitArtifactOpenRequest(event, "retained-session");
  clearArtifactOpenRequest("cleared-session");
  const cleared = vi.fn();
  const retained = vi.fn();
  const stopCleared = onArtifactOpenRequest("cleared-session", cleared);
  const stopRetained = onArtifactOpenRequest("retained-session", retained);
  expect(cleared).not.toHaveBeenCalled();
  expect(retained).toHaveBeenCalledWith(event, "retained-session");
  emitArtifactOpenRequest({ ...event, path: "new.html" }, "cleared-session");
  expect(cleared).toHaveBeenCalledWith({ ...event, path: "new.html" }, "cleared-session");
  stopCleared();
  stopRetained();
});

it("retains only the latest request per session until its subscriber appears", () => {
  const event = (path: string) => ({
    type: "artifact_open_request" as const,
    path,
    base: "workspace" as const,
  });
  emitArtifactOpenRequest(event("old"), "bus-a");
  emitArtifactOpenRequest(event("new"), "bus-a");
  emitArtifactOpenRequest(event("other"), "bus-b");
  const listener = vi.fn();
  const stop = onArtifactOpenRequest("bus-a", listener);
  expect(listener).toHaveBeenCalledTimes(1);
  expect(listener).toHaveBeenCalledWith(event("new"), "bus-a");
  stop();
  const other = vi.fn();
  const stopOther = onArtifactOpenRequest("bus-b", other);
  expect(other).toHaveBeenCalledWith(event("other"), "bus-b");
  stopOther();
});
