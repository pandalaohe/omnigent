// External-access settings for artifact links: one switch and one write-only
// share code. The page learns only whether a code is set, never the code.
// A failed request keeps the last known state and raises the app's error toast.

import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { showToast } from "@/components/ui/toast";
import { authenticatedFetch } from "@/lib/identity";
import { SettingRow } from "@/components/SettingsFields";

/** Response shape of `GET`/`PUT /v1/artifact-sharing`. */
interface SharingState {
  external: boolean;
  share_code_set: boolean;
  /** Whether shared-link visitors may leave comments. Absent on older
   *  servers that predate the switch; treated as on (the server default). */
  allow_comments?: boolean;
}

const MIN_SHARE_CODE_LENGTH = 4;
const MAX_SHARE_CODE_LENGTH = 64;

async function requestSharing(init?: RequestInit): Promise<SharingState> {
  const res = await authenticatedFetch("/v1/artifact-sharing", init);
  if (!res.ok) throw new Error(`artifact-sharing request failed (${res.status})`);
  return (await res.json()) as SharingState;
}

export function ArtifactSharingSettings() {
  const [state, setState] = useState<SharingState | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [codeDraft, setCodeDraft] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    requestSharing().then(
      (next) => {
        if (!cancelled) setState(next);
      },
      () => {
        if (cancelled) return;
        setLoadFailed(true);
        showToast("Couldn't load link settings.", { duration: 0 });
      },
    );
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  const save = useCallback(
    async (patch: {
      external?: boolean;
      share_code?: string | null;
      allow_comments?: boolean;
    }): Promise<boolean> => {
      setSaving(true);
      try {
        setState(
          await requestSharing({
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(patch),
          }),
        );
        return true;
      } catch {
        showToast("Couldn't save link settings.", { duration: 0 });
        return false;
      } finally {
        setSaving(false);
      }
    },
    [],
  );

  const trimmedCode = codeDraft.trim();
  const canSaveCode =
    trimmedCode.length >= MIN_SHARE_CODE_LENGTH && trimmedCode.length <= MAX_SHARE_CODE_LENGTH;

  if (!state) {
    return loadFailed ? (
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => setReloadKey((key) => key + 1)}
      >
        Retry
      </Button>
    ) : null;
  }

  return (
    <div className="flex flex-col">
      <SettingRow
        label="External access"
        hint="Let visitors open shared file links outside the app."
      >
        <Switch
          aria-label="External access"
          checked={state.external}
          disabled={saving}
          onCheckedChange={(external) => void save({ external })}
          className="shrink-0"
        />
      </SettingRow>
      <div className="mt-5 flex flex-wrap items-center justify-between gap-x-6 gap-y-3 border-t border-border pt-5">
        <label
          htmlFor="artifact-share-code"
          className="text-ui font-normal text-foreground md:font-medium"
        >
          Share code
        </label>
        <div className="flex max-w-full flex-wrap items-center gap-2">
          <Input
            id="artifact-share-code"
            type="password"
            autoComplete="off"
            value={codeDraft}
            disabled={saving || !state.external}
            placeholder="4 to 64 characters"
            aria-describedby={!state.external ? "artifact-share-code-disabled" : undefined}
            onChange={(event) => setCodeDraft(event.target.value)}
            className="h-9 w-48 max-w-full font-mono text-sm"
          />
          <Button
            type="button"
            size="sm"
            disabled={!canSaveCode || saving || !state.external}
            onClick={() => {
              void save({ share_code: trimmedCode }).then((saved) => {
                if (saved) setCodeDraft("");
              });
            }}
          >
            Save code
          </Button>
          {state.share_code_set && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={saving || !state.external}
              onClick={() => void save({ share_code: null })}
            >
              Clear code
            </Button>
          )}
        </div>
      </div>
      {!state.external && (
        <p id="artifact-share-code-disabled" className="mt-2 text-sm text-muted-foreground">
          Turn on External access to change the share code.
        </p>
      )}
      <div className="mt-5 border-t border-border pt-5">
        <SettingRow label="Visitor comments" hint="Let visitors comment on shared files.">
          <Switch
            aria-label="Visitor comments"
            checked={state.allow_comments ?? true}
            disabled={saving}
            onCheckedChange={(allow_comments) => void save({ allow_comments })}
            className="shrink-0"
          />
        </SettingRow>
      </div>
    </div>
  );
}
