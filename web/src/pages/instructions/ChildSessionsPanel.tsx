import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ChildIdleSettings } from "@/components/ChildIdleSettings";
import { OfflineHostArchiveSettings } from "@/components/OfflineHostArchiveSettings";
import { SettingRow } from "@/components/SettingsFields";
import { StandingApprovalDefaults } from "@/components/StandingApprovalDefaults";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { authenticatedFetch, getCurrentUserId, subscribeIdentity } from "@/lib/identity";
import {
  CHILD_IDLE_CHANGED_EVENT,
  CHILD_IDLE_STORAGE_KEY,
  readChildIdlePreferences,
  writeChildIdlePreferences,
} from "@/lib/childIdlePreferences";
import { useSessionCollabPreferences } from "@/hooks/useSessionCollabPreferences";
import {
  readSessionCollabPreferences,
  writeSessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";
import { USER_PREFERENCES_PATCH_REJECTED_EVENT } from "@/lib/userPreferencesSync";

export function ChildSessionsPanel({ openingControls }: { openingControls?: ReactNode }) {
  const instructionsId = useId();
  const preferences = useSessionCollabPreferences();
  const [cap, setCap] = useState(preferences.childCap?.toString() ?? "");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => setCap(preferences.childCap?.toString() ?? ""), [preferences.childCap]);
  const [instructionDraft, setInstructionDraft] = useState(() => {
    const value = readChildIdlePreferences().checkinInstructions;
    return { stored: value, value };
  });
  const instructions = instructionDraft.value;
  const instructionsOwner = useRef(getCurrentUserId());
  const [defaultInstructions, setDefaultInstructions] = useState<string | null>(null);
  const [defaultsError, setDefaultsError] = useState(false);
  const [defaultsAttempt, setDefaultsAttempt] = useState(0);
  const [instructionsError, setInstructionsError] = useState<string | null>(null);
  const [instructionsSaved, setInstructionsSaved] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setDefaultsError(false);
    void authenticatedFetch("/v1/child-checkin/defaults", { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("Could not load check-in defaults");
        const body = await response.json();
        if (typeof body.instructions !== "string") throw new Error("Invalid check-in defaults");
        if (!controller.signal.aborted) setDefaultInstructions(body.instructions);
      })
      .catch(() => {
        if (!controller.signal.aborted) setDefaultsError(true);
      });
    return () => controller.abort();
  }, [defaultsAttempt]);

  useEffect(() => {
    const refresh = () => {
      const stored = readChildIdlePreferences().checkinInstructions;
      const owner = getCurrentUserId();
      const ownerChanged = owner !== instructionsOwner.current;
      instructionsOwner.current = owner;
      setInstructionDraft((current) => ({
        stored,
        value: ownerChanged || current.value === current.stored ? stored : current.value,
      }));
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key === CHILD_IDLE_STORAGE_KEY) refresh();
    };
    const unsubscribe = subscribeIdentity(refresh);
    window.addEventListener(CHILD_IDLE_CHANGED_EVENT, refresh);
    window.addEventListener("storage", onStorage);
    return () => {
      unsubscribe();
      window.removeEventListener(CHILD_IDLE_CHANGED_EVENT, refresh);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const instructionsText = instructions ?? defaultInstructions ?? "";
  const instructionsLength = [...instructionsText].length;
  const changeInstructions = (value: string | null) => {
    setInstructionDraft((current) => ({ ...current, value }));
    setInstructionsError(null);
    setInstructionsSaved(false);
  };
  const saveInstructions = () => {
    if (instructions !== null && [...instructions].length > 4000) {
      setInstructionsError("Check-in instructions must be at most 4000 characters.");
      return;
    }
    if (instructions && !instructions.trim()) {
      setInstructionsError("Enter instructions or choose Send no instructions.");
      return;
    }
    setInstructionDraft({ stored: instructions, value: instructions });
    writeChildIdlePreferences({
      ...readChildIdlePreferences(),
      checkinInstructions: instructions,
    });
    setInstructionsError(null);
    setInstructionsSaved(true);
  };

  useEffect(() => {
    const rejected = (event: Event) => {
      const namespace = (event as CustomEvent<{ namespace: string }>).detail?.namespace;
      if (namespace === "session_collab") {
        setError("The server rejected these settings. Review the values and save again.");
        setSaved(false);
      } else if (namespace === "child_idle") {
        setInstructionsError(
          "The server rejected these check-in settings. Review the values and save again.",
        );
        setInstructionsSaved(false);
      }
    };
    window.addEventListener(USER_PREFERENCES_PATCH_REJECTED_EVENT, rejected);
    return () => window.removeEventListener(USER_PREFERENCES_PATCH_REJECTED_EVENT, rejected);
  }, []);
  const save = () => {
    const text = cap.trim();
    const value = text === "" ? null : Number(text);
    if (text && (!/^[1-9]\d*$/.test(text) || !Number.isSafeInteger(value))) {
      setError("Child limit must be a whole number of at least 1, or blank for unlimited.");
      return;
    }
    writeSessionCollabPreferences({ ...readSessionCollabPreferences(), childCap: value });
    setError(null);
    setSaved(true);
  };
  return (
    <div className="max-w-2xl space-y-6">
      <section aria-label="Opening" className="space-y-4">
        <h3 className="text-ui font-semibold">Opening</h3>
        <SettingRow label="Open children per parent" hint="Blank allows unlimited children.">
          <Input
            aria-label="Open children per parent"
            className="h-9 w-24"
            inputMode="numeric"
            placeholder="Unlimited"
            value={cap}
            onChange={(event) => {
              setCap(event.target.value);
              setError(null);
              setSaved(false);
            }}
          />
          <span className="text-sm text-muted-foreground">children</span>
        </SettingRow>
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Button type="button" onClick={save}>
            Save child settings
          </Button>
          {saved && (
            <span role="status" className="text-sm text-muted-foreground">
              Saved preferences.
            </span>
          )}
        </div>
        {error && (
          <p role="alert" className="mt-3 text-sm text-destructive">
            {error}
          </p>
        )}
        <StandingApprovalDefaults />
        {openingControls}
      </section>
      <section
        id="child-idle"
        aria-label="Check-ins"
        className="space-y-4 border-t border-border pt-4"
      >
        <h3 className="text-ui font-semibold">Check-ins</h3>
        <ChildIdleSettings />
        <details>
          <summary className="cursor-pointer text-ui font-medium">Check-in instructions</summary>
          <div className="mt-3 space-y-3">
            <label htmlFor={instructionsId} className="sr-only">
              Check-in instructions
            </label>
            <p id={`${instructionsId}-help`} className="text-sm text-muted-foreground">
              Guide how parent sessions handle fallback check-ins. Changes apply to the next notice
              and grant no permissions.
            </p>
            <Textarea
              id={instructionsId}
              aria-describedby={`${instructionsId}-help ${instructionsId}-count`}
              aria-invalid={Boolean(instructionsError)}
              className="h-28 min-h-24 max-h-64 resize-y overflow-y-auto field-sizing-fixed"
              rows={4}
              value={instructionsText}
              disabled={instructions === null && defaultInstructions === null}
              placeholder={
                defaultsError ? "Default instructions unavailable" : "Loading default instructions…"
              }
              onChange={(event) => changeInstructions(event.target.value.replaceAll("\0", ""))}
            />
            <div className="flex flex-wrap items-center gap-2">
              <Button type="button" variant="outline" onClick={() => changeInstructions(null)}>
                Reset to default
              </Button>
              <Button type="button" variant="outline" onClick={() => changeInstructions("")}>
                Send no instructions
              </Button>
              <span
                id={`${instructionsId}-count`}
                className="ml-auto text-xs text-muted-foreground"
              >
                {instructionsLength} / 4000 characters
              </span>
            </div>
            {defaultsError && (
              <p role="alert" className="text-sm text-destructive">
                Could not load default instructions.{" "}
                <button
                  type="button"
                  className="underline"
                  onClick={() => setDefaultsAttempt((value) => value + 1)}
                >
                  Retry
                </button>
              </p>
            )}
            <div className="flex flex-wrap items-center gap-3">
              <Button type="button" onClick={saveInstructions}>
                Save check-in instructions
              </Button>
              {instructionsSaved && (
                <span role="status" className="text-sm text-muted-foreground">
                  Saved instructions.
                </span>
              )}
            </div>
          </div>
        </details>
        {instructionsError && (
          <p role="alert" className="text-sm text-destructive">
            {instructionsError}
          </p>
        )}
      </section>
      <section aria-label="Offline hosts" className="space-y-4 border-t border-border pt-4">
        <h3 className="text-ui font-semibold">Offline hosts</h3>
        <OfflineHostArchiveSettings />
      </section>
    </div>
  );
}
