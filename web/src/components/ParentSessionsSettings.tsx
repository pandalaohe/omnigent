import { useEffect, useRef, useState } from "react";

import { SettingRow } from "@/components/SettingsFields";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { useSessionCollabPreferences } from "@/hooks/useSessionCollabPreferences";
import { t } from "@/lib/autoRotationText";
import {
  readSessionCollabPreferences,
  SESSION_COLLAB_BOUNDS,
  writeSessionCollabPreferences,
  type SessionCollabPreferences,
} from "@/lib/sessionCollabPreferences";
import { USER_PREFERENCES_PATCH_REJECTED_EVENT } from "@/lib/userPreferencesSync";

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
  const [saved, setSaved] = useState(false);

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
      setSaved(false);
      setError(
        typeof detail.status === "number"
          ? t("serverRejectedSettingsError", { status: detail.status })
          : t("serverRejectedSettingsGenericError"),
      );
    };
    window.addEventListener(USER_PREFERENCES_PATCH_REJECTED_EVENT, onRejected);
    return () => window.removeEventListener(USER_PREFERENCES_PATCH_REJECTED_EVENT, onRejected);
  }, []);

  const save = () => {
    const compactionsBounds = SESSION_COLLAB_BOUNDS.autoRotationCompactions;
    const compactions = parseBoundedInteger(
      draft.autoRotationCompactions,
      compactionsBounds.min,
      compactionsBounds.max,
    );
    if (compactions === null) {
      setSaved(false);
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
      setSaved(false);
      setError(t("remainingPctRangeError", remainingBounds));
      return;
    }

    const nextDraft: ParentSessionsDraft = {
      autoRotationEnabled: draft.autoRotationEnabled,
      autoRotationCompactions: String(compactions),
      autoRotationRemainingPct: String(remainingPct),
    };
    storedDraft.current = JSON.stringify(nextDraft);
    setDraft(nextDraft);
    writeSessionCollabPreferences({
      ...readSessionCollabPreferences(),
      autoRotationEnabled: draft.autoRotationEnabled,
      autoRotationCompactions: compactions,
      autoRotationRemainingPct: remainingPct,
    });
    setError(null);
    setSaved(true);
  };

  return (
    <div className="max-w-2xl space-y-4" data-testid="parent-sessions-settings">
      <SettingRow label={t("autoRotationLabel")} hint={t("autoRotationHint")}>
        <Switch
          aria-label={t("autoRotationLabel")}
          checked={draft.autoRotationEnabled}
          onCheckedChange={(checked) => {
            setDraft((current) => ({ ...current, autoRotationEnabled: checked }));
            setError(null);
            setSaved(false);
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
            setDraft((current) => ({ ...current, autoRotationCompactions: event.target.value }));
            setError(null);
            setSaved(false);
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
            setDraft((current) => ({ ...current, autoRotationRemainingPct: event.target.value }));
            setError(null);
            setSaved(false);
          }}
        />
        <span className="text-sm text-muted-foreground">{t("percentUnit")}</span>
      </SettingRow>

      <p className="text-sm text-muted-foreground">{t("approximateHarnessesNote")}</p>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          type="button"
          data-testid="parent-sessions-save"
          data-state={saved ? "saved" : "idle"}
          onClick={save}
        >
          {t("saveButton")}
        </Button>
        {saved && (
          <span role="status" className="text-sm text-muted-foreground">
            {t("savedStatus")}
          </span>
        )}
        {error && (
          <span role="alert" className="text-sm text-destructive">
            {error}
          </span>
        )}
      </div>
    </div>
  );
}
