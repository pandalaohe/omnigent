/**
 * Admin global-instructions page (``/settings/global-instructions``).
 *
 * One text saved here is appended to the instructions of every session
 * started afterwards; edits never reach a running session. Rendered as a
 * Settings sub-category.
 *
 * Gated on the client by an early admin check (non-admins see a "no
 * permission" message) AND on the server by the route handlers themselves —
 * client-side gating is just UX.
 */

import { useEffect, useRef, useState } from "react";
import { SaveIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  useGlobalInstructions,
  useGlobalInstructionRevisions,
  useSaveGlobalInstructions,
} from "@/hooks/useGlobalInstructions";
import { getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isSingleUserMode } from "@/lib/capabilities";
import { absoluteTime } from "@/lib/relativeTime";

export function GlobalInstructionsPage() {
  const info = useServerInfo();
  // Explicit single-user local runtime: no auth endpoints exist, so skip the
  // admin probe (same rule as the Policies page).
  const isSingleUser = isSingleUserMode(info);
  const [meIsAdmin, setMeIsAdmin] = useState<boolean | null>(null);
  const { data, isError: isLoadError, error: loadError } = useGlobalInstructions();
  const revisions = useGlobalInstructionRevisions(isSingleUser || meIsAdmin === true);
  const save = useSaveGlobalInstructions();
  const [draft, setDraft] = useState("");
  // Server text the draft was last synced from. "" is the untouched initial
  // draft, so edits typed before the first GET resolves survive it.
  const lastSyncedText = useRef("");

  useEffect(() => {
    if (!data) return;
    const synced = lastSyncedText.current;
    setDraft((current) => (current === synced ? data.text : current));
    lastSyncedText.current = data.text;
  }, [data]);

  useEffect(() => {
    if (isSingleUser) return;
    void (async () => {
      const userId = await resolveIdentity();
      if (userId === null) return;
      setMeIsAdmin(getCurrentIsAdmin());
    })();
  }, [isSingleUser]);

  if (!isSingleUser && meIsAdmin === null) {
    return (
      <div className="flex min-h-full items-center justify-center text-ui text-muted-foreground">
        Loading...
      </div>
    );
  }

  if (!isSingleUser && meIsAdmin === false) {
    return (
      <PageScroll contentClassName="px-8" extraBottom="2.5rem">
        <h1 className="mb-2 text-2xl font-semibold">Global Instructions</h1>
        <p className="text-ui text-muted-foreground">
          You don't have permission to manage global instructions.
        </p>
      </PageScroll>
    );
  }

  const maxChars = data?.max_chars ?? 0;
  const draftLength = [...draft].length;
  const overCap = data !== undefined && draftLength > maxChars;
  const unchanged = data !== undefined && draft === data.text;
  const savedAtMs = data?.updated_at != null ? data.updated_at * 1000 : null;
  const revisionList = revisions.data ?? [];

  return (
    <PageScroll contentClassName="px-8" extraBottom="2.5rem">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold">Global Instructions</h1>
        <p className="mt-1 text-ui text-muted-foreground">
          Added to the instructions of every session started afterwards.
        </p>
      </div>

      <div className="flex flex-col gap-3">
        <Textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          aria-label="Global instructions"
          aria-invalid={overCap}
          className="min-h-64 font-mono text-sm"
        />
        <div className="flex items-center justify-between gap-4">
          <div className="flex flex-col gap-1">
            <span
              className={
                overCap ? "text-sm font-medium text-destructive" : "text-sm text-muted-foreground"
              }
            >
              {draftLength} / {maxChars}
            </span>
            {savedAtMs !== null ? (
              <span className="text-sm text-muted-foreground">
                Last saved {absoluteTime(savedAtMs)}
                {data?.updated_by != null && ` by ${data.updated_by}`}
              </span>
            ) : (
              <span className="text-sm text-muted-foreground">No instructions saved yet.</span>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button
              onClick={() => save.mutate(draft)}
              loading={save.isPending}
              disabled={data === undefined || overCap || unchanged || save.isPending}
            >
              <SaveIcon /> Save
            </Button>
          </div>
        </div>

        <p className="text-sm text-muted-foreground">
          Changes apply to sessions started after saving — running sessions keep the text they
          started with — and the text is visible to every agent and user on this server.
        </p>

        {overCap && (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-ui text-destructive"
          >
            Over the {maxChars}-character limit by {draftLength - maxChars} characters.
          </div>
        )}

        {isLoadError && (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-ui text-destructive"
          >
            {loadError.message}
          </div>
        )}

        {save.isError && (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-ui text-destructive"
          >
            {save.error.message}
          </div>
        )}
      </div>

      {revisionList.length > 0 && (
        <div className="mt-8">
          <h2 className="mb-2 text-ui font-medium">History</h2>
          <div className="flex flex-col divide-y divide-border rounded-lg border border-border">
            {revisionList.map((revision) => (
              <button
                key={revision.id}
                type="button"
                onClick={() => setDraft(revision.text)}
                className="flex flex-col gap-0.5 px-3 py-2 text-left hover:bg-muted"
              >
                <span className="text-sm text-muted-foreground">
                  {absoluteTime(revision.created_at * 1000)}
                  {revision.created_by != null && ` · ${revision.created_by}`}
                </span>
                <span className="line-clamp-2 whitespace-pre-wrap text-sm">
                  {revision.text || "(empty)"}
                </span>
              </button>
            ))}
          </div>
          <p className="mt-2 text-sm text-muted-foreground">
            Selecting a revision loads it into the editor; Save stores it as a new revision.
          </p>
        </div>
      )}
    </PageScroll>
  );
}
