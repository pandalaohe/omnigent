import { useEffect, useRef, useState } from "react";

import { SettingRow } from "@/components/SettingsFields";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { useSessionCollabPreferences } from "@/hooks/useSessionCollabPreferences";
import { t } from "@/lib/autoRotationText";
import {
  readSessionCollabPreferences,
  saveSessionCollabPreferences,
  SESSION_COLLAB_BOUNDS,
  type SessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";
import { USER_PREFERENCES_PATCH_REJECTED_EVENT } from "@/lib/userPreferencesSync";

type SaveState = "idle" | "pending" | "saved" | "failed";

interface ParentSessionsDraft {
  autoRotationEnabled: boolean;
  autoRotationCompactions: string;
  autoRotationRemainingPct: string;
}

function draftFromPreferences(preferences: SessionCollabPreferences): ParentSessionsDraft {
  return {
    autoRotationEnabled: preferences.autoRotationEnabled,
    autoRotationCompactions: String(preferences.autoRotationCompactions),
    autoRotationRemainingPct: String(preferences.autoRotationRemainingPct),
  };
}

function parseBoundedInteger(text: string, min: number, max: number): number | null {
  const trimmed = text.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  if (!Number.isSafeInteger(value) || value < min || value > max) return null;
  return value;
}

export function ParentSessionsSettings() {
  const preferences = useSessionCollabPreferences();
  const [draft, setDraft] = useState<ParentSessionsDraft>(() => draftFromPreferences(preferences));
  const storedDraft = useRef(JSON.stringify(draftFromPreferences(preferences)));
  const [error, setError] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const saveAbortRef = useRef<AbortController | null>(null);
  const saveSeqRef = useRef(0);

  const clearPendingSave = () => {
    saveAbortRef.current?.abort();
    saveAbortRef.current = null;
    saveSeqRef.current += 1;
  };

  useEffect(() => () => clearPendingSave(), []);

  useEffect(() => {
    const incoming = draftFromPreferences(preferences);
    const incomingKey = JSON.stringify(incoming);
    if (storedDraft.current !== incomingKey) {
      const isClean = JSON.stringify(draft) === storedDraft.current;
      storedDraft.current = incomingKey;
      if (isClean) {
        setDraft(incoming);
      }
    }
  }, [preferences, draft]);

  useEffect(() => {
    const onRejected = (event: Event) => {
      const detail = (event as CustomEvent<{ namespace?: string; status?: number }>).detail;
      if (detail?.namespace !== "session_collab") return;
      clearPendingSave();
      setSaveState("failed");
      setError(
        typeof detail.status === "number"
          ? t("serverRejectedSettingsError", { status: detail.status })
          : t("serverRejectedSettingsGenericError"),
      );
    };
    window.addEventListener(USER_PREFERENCES_PATCH_REJECTED_EVENT, onRejected);
    return () => window.removeEventListener(USER_PREFERENCES_PATCH_REJECTED_EVENT, onRejected);
  }, []);

  const save = async () => {
    const compactionsBounds = SESSION_COLLAB_BOUNDS.autoRotationCompactions;
    const compactions = parseBoundedInteger(
      draft.autoRotationCompactions,
      compactionsBounds.min,
      compactionsBounds.max,
    );
    if (compactions === null) {
      clearPendingSave();
      setSaveState("failed");
      setError(t("compactionsRangeError", compactionsBounds));
      return;
    }

    const remainingBounds = SESSION_COLLAB_BOUNDS.autoRotationRemainingPct;
    const remainingPct = parseBoundedInteger(
      draft.autoRotationRemainingPct,
      remainingBounds.min,
      remainingBounds.max,
    );
    if (remainingPct === null) {
      clearPendingSave();
      setSaveState("failed");
      setError(t("remainingPctRangeError", remainingBounds));
      return;
    }

    saveAbortRef.current?.abort();
    const controller = new AbortController();
    saveAbortRef.current = controller;
    const seq = ++saveSeqRef.current;

    const nextDraft: ParentSessionsDraft = {
      autoRotationEnabled: draft.autoRotationEnabled,
      autoRotationCompactions: String(compactions),
      autoRotationRemainingPct: String(remainingPct),
    };
    storedDraft.current = JSON.stringify(nextDraft);
    setDraft(nextDraft);
    setError(null);
    setSaveState("pending");

    try {
      await saveSessionCollabPreferences(
        {
          ...readSessionCollabPreferences(),
          autoRotationEnabled: draft.autoRotationEnabled,
          autoRotationCompactions: compactions,
          autoRotationRemainingPct: remainingPct,
        },
        controller.signal,
      );
      if (saveSeqRef.current !== seq || controller.signal.aborted) return;
      setError(null);
      setSaveState("saved");
    } catch (reason) {
      if (saveSeqRef.current !== seq || controller.signal.aborted) return;
      const status =
        reason &&
        typeof reason === "object" &&
        "status" in reason &&
        typeof (reason as { status?: unknown }).status === "number"
          ? (reason as { status: number }).status
          : undefined;
      setSaveState("failed");
      setError(
        status !== undefined
          ? t("serverRejectedSettingsError", { status })
          : t("serverRejectedSettingsGenericError"),
      );
    }
  };

  return (
    <div className="max-w-2xl space-y-4" data-testid="parent-sessions-settings">
      <SettingRow label={t("autoRotationLabel")} hint={t("autoRotationHint")}>
        <Switch
          aria-label={t("autoRotationLabel")}
          checked={draft.autoRotationEnabled}
          onCheckedChange={(checked) => {
            clearPendingSave();
            setDraft((current) => ({ ...current, autoRotationEnabled: checked }));
            setError(null);
            setSaveState("idle");
          }}
          data-testid="parent-sessions-auto-rotation-switch"
        />
      </SettingRow>

      <SettingRow label={t("rotateAfterCompactionsLabel")} hint={t("rotateAfterCompactionsHint")}>
        <Input
          type="number"
          inputMode="numeric"
          min={SESSION_COLLAB_BOUNDS.autoRotationCompactions.min}
          max={SESSION_COLLAB_BOUNDS.autoRotationCompactions.max}
          step={1}
          aria-label={t("rotateAfterCompactionsLabel")}
          data-testid="parent-sessions-compactions-input"
          className="h-9 w-20"
          value={draft.autoRotationCompactions}
          onChange={(event) => {
            clearPendingSave();
            setDraft((current) => ({ ...current, autoRotationCompactions: event.target.value }));
            setError(null);
            setSaveState("idle");
          }}
        />
      </SettingRow>

      <SettingRow label={t("contextLeftAtRotationLabel")} hint={t("contextLeftAtRotationHint")}>
        <Input
          type="number"
          inputMode="numeric"
          min={SESSION_COLLAB_BOUNDS.autoRotationRemainingPct.min}
          max={SESSION_COLLAB_BOUNDS.autoRotationRemainingPct.max}
          step={1}
          aria-label={t("contextLeftAtRotationLabel")}
          data-testid="parent-sessions-remaining-pct-input"
          className="h-9 w-20"
          value={draft.autoRotationRemainingPct}
          onChange={(event) => {
            clearPendingSave();
            setDraft((current) => ({ ...current, autoRotationRemainingPct: event.target.value }));
            setError(null);
            setSaveState("idle");
          }}
        />
        <span className="text-sm text-muted-foreground">{t("percentUnit")}</span>
      </SettingRow>

      <p className="text-sm text-muted-foreground">{t("approximateHarnessesNote")}</p>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          type="button"
          data-testid="parent-sessions-save"
          data-state={saveState}
          disabled={saveState === "pending"}
          onClick={() => void save()}
        >
          {saveState === "pending" ? t("savingButton") : t("saveButton")}
        </Button>
        {saveState === "pending" && (
          <span role="status" data-state="pending" className="text-sm text-muted-foreground">
            {t("savingButton")}
          </span>
        )}
        {saveState === "saved" && (
          <span role="status" data-state="saved" className="text-sm text-muted-foreground">
            {t("savedStatus")}
          </span>
        )}
        {error && (
          <span role="alert" data-state="failed" className="text-sm text-destructive">
            {error}
          </span>
        )}
      </div>
    </div>
  );
}
