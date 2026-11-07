import { useEffect, useId, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCwIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useSessionCollabPreferences } from "@/hooks/useSessionCollabPreferences";
import {
  getAutoRotation,
  patchAutoRotation,
  type AutoRotationPatch,
  type AutoRotationStatus,
} from "@/lib/autoRotationApi";
import { t } from "@/lib/autoRotationText";
import {
  SESSION_COLLAB_BOUNDS,
  SESSION_COLLAB_CHANGED_EVENT,
  SESSION_COLLAB_STORAGE_KEY,
} from "@/lib/sessionCollabPreferences";

interface SessionAutoRotationDraft {
  enabled: "default" | "on" | "off";
  compactions: string;
  remainingPct: string;
}

function draftFromStatus(status: AutoRotationStatus): SessionAutoRotationDraft {
  return {
    enabled: status.sources.enabled === "session" ? (status.enabled ? "on" : "off") : "default",
    compactions: status.sources.compactions === "session" ? String(status.compactions_setting) : "",
    remainingPct:
      status.sources.remaining_pct === "session" ? String(status.remaining_pct_setting) : "",
  };
}

function parseOptionalBoundedInteger(
  text: string,
  min: number,
  max: number,
): number | "default" | null {
  const trimmed = text.trim();
  if (trimmed === "") return "default";
  if (!/^\d+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  if (!Number.isSafeInteger(value) || value < min || value > max) return null;
  return value;
}

export function AutoRotationChip({
  sessionId,
  readOnly = false,
}: {
  sessionId: string;
  readOnly?: boolean;
}) {
  const client = useQueryClient();
  const preferences = useSessionCollabPreferences();
  const enabledSelectId = useId();
  const compactionsInputId = useId();
  const remainingPctInputId = useId();

  const query = useQuery({
    queryKey: ["auto-rotation", sessionId],
    queryFn: ({ signal }) => getAutoRotation(sessionId, signal),
    retry: false,
    staleTime: 15_000,
  });

  const [draft, setDraft] = useState<SessionAutoRotationDraft>({
    enabled: "default",
    compactions: "",
    remainingPct: "",
  });
  const storedSessionId = useRef(sessionId);
  const storedStatusKey = useRef<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const status = query.data;

  useEffect(() => {
    const sessionChanged = storedSessionId.current !== sessionId;
    if (sessionChanged) {
      storedSessionId.current = sessionId;
      storedStatusKey.current = null;
      setError(null);
      setSaved(false);
    }
    if (!status || status.eligible !== true || !status.sources) {
      if (sessionChanged) {
        setDraft({ enabled: "default", compactions: "", remainingPct: "" });
      }
      return;
    }
    const incoming = draftFromStatus(status);
    const incomingKey = JSON.stringify(incoming);
    if (sessionChanged || storedStatusKey.current !== incomingKey) {
      const isClean =
        sessionChanged ||
        storedStatusKey.current === null ||
        JSON.stringify(draft) === storedStatusKey.current;
      storedStatusKey.current = incomingKey;
      if (isClean) {
        setDraft(incoming);
      }
    }
  }, [sessionId, status, draft]);

  const saveMutation = useMutation({
    mutationFn: (patch: AutoRotationPatch) => patchAutoRotation(sessionId, patch),
    onMutate: () => client.cancelQueries({ queryKey: ["auto-rotation", sessionId] }),
    onSuccess: async (nextStatus) => {
      await client.cancelQueries({ queryKey: ["auto-rotation", sessionId] });
      client.setQueryData(["auto-rotation", sessionId], nextStatus);
      const nextDraft = draftFromStatus(nextStatus);
      storedStatusKey.current = JSON.stringify(nextDraft);
      setDraft(nextDraft);
      setError(null);
      setSaved(true);
    },
    onError: (reason) => {
      setSaved(false);
      setError(reason instanceof Error ? reason.message : t("saveSessionError"));
    },
  });

  useEffect(() => {
    const invalidate = () => {
      void client.invalidateQueries({ queryKey: ["auto-rotation", sessionId] });
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key === SESSION_COLLAB_STORAGE_KEY) invalidate();
    };
    window.addEventListener(SESSION_COLLAB_CHANGED_EVENT, invalidate);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(SESSION_COLLAB_CHANGED_EVENT, invalidate);
      window.removeEventListener("storage", onStorage);
    };
  }, [client, sessionId]);

  if (!status || status.eligible !== true || !status.sources) {
    return null;
  }

  const defaultEnabled =
    status.sources.enabled !== "session" ? status.enabled : preferences.autoRotationEnabled;
  const defaultCompactions =
    status.sources.compactions !== "session"
      ? status.compactions_setting
      : preferences.autoRotationCompactions;
  const defaultRemainingPct =
    status.sources.remaining_pct !== "session"
      ? status.remaining_pct_setting
      : preferences.autoRotationRemainingPct;

  const handleSave = () => {
    const compactionsBounds = SESSION_COLLAB_BOUNDS.autoRotationCompactions;
    const compactions = parseOptionalBoundedInteger(
      draft.compactions,
      compactionsBounds.min,
      compactionsBounds.max,
    );
    if (compactions === null) {
      setSaved(false);
      setError(t("compactionsRangeError", compactionsBounds));
      return;
    }

    const remainingBounds = SESSION_COLLAB_BOUNDS.autoRotationRemainingPct;
    const remainingPct = parseOptionalBoundedInteger(
      draft.remainingPct,
      remainingBounds.min,
      remainingBounds.max,
    );
    if (remainingPct === null) {
      setSaved(false);
      setError(t("remainingPctRangeError", remainingBounds));
      return;
    }

    setError(null);
    saveMutation.mutate({
      enabled: draft.enabled,
      compactions,
      remaining_pct: remainingPct,
    });
  };

  return (
    <Popover
      onOpenChange={(nextOpen) => {
        if (nextOpen && query.isStale) void query.refetch();
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid="auto-rotation-chip-trigger"
          aria-label={
            status.enabled
              ? t("chipAriaLabelActive", {
                  compactions: status.compactions,
                  limit: status.compactions_setting,
                })
              : t("chipAriaLabelOff")
          }
          className="inline-flex h-7 shrink-0 items-center justify-center gap-1 rounded-full border border-border bg-background px-2 text-xs leading-none text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <RefreshCwIcon className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="hidden sm:inline">{t("chipLabel")}</span>
          <span>
            {status.enabled
              ? t("chipValueRatio", {
                  compactions: status.compactions,
                  limit: status.compactions_setting,
                })
              : t("chipValueOff")}
          </span>
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 max-w-[calc(100vw-1rem)] space-y-3 p-4">
        <h2 className="font-medium text-foreground">{t("popoverTitle")}</h2>
        <div className="space-y-1 text-xs text-muted-foreground">
          <p>
            {t("compactionsSummary", {
              compactions: status.compactions,
              limit: status.compactions_setting,
            })}
          </p>
          <p>
            {status.remaining_pct !== null
              ? t("contextLeftSummary", { remainingPct: status.remaining_pct })
              : t("contextNotReportedSummary")}
          </p>
          {status.approximate.length > 0 && <p>{t("approximateSessionNote")}</p>}
          {status.rotation_pending && (
            <p className="font-medium text-foreground">{t("continuesNextTurnNotice")}</p>
          )}
        </div>

        {readOnly ? (
          <p className="text-xs text-muted-foreground">{t("readOnlyNotice")}</p>
        ) : (
          <div className="space-y-2.5 border-t border-border pt-3">
            <div className="flex items-center justify-between gap-3">
              <label htmlFor={enabledSelectId} className="text-xs font-medium text-foreground">
                {t("autoRotationLabel")}
              </label>
              <select
                id={enabledSelectId}
                aria-label={t("autoRotationLabel")}
                data-testid="auto-rotation-chip-enabled-select"
                disabled={saveMutation.isPending}
                value={draft.enabled}
                onChange={(event) => {
                  setDraft((current) => ({
                    ...current,
                    enabled: event.target.value as "default" | "on" | "off",
                  }));
                  setError(null);
                  setSaved(false);
                }}
                className="h-8 rounded-md border border-border bg-background px-2 text-xs text-foreground"
              >
                <option value="default">
                  {defaultEnabled ? t("useDefaultOnOption") : t("useDefaultOffOption")}
                </option>
                <option value="on">{t("onOption")}</option>
                <option value="off">{t("offOption")}</option>
              </select>
            </div>

            <div className="flex items-center justify-between gap-3">
              <label htmlFor={compactionsInputId} className="text-xs font-medium text-foreground">
                {t("rotateAfterCompactionsLabel")}
              </label>
              <Input
                id={compactionsInputId}
                type="number"
                inputMode="numeric"
                min={SESSION_COLLAB_BOUNDS.autoRotationCompactions.min}
                max={SESSION_COLLAB_BOUNDS.autoRotationCompactions.max}
                step={1}
                aria-label={t("rotateAfterCompactionsLabel")}
                data-testid="auto-rotation-chip-compactions-input"
                disabled={saveMutation.isPending}
                placeholder={String(defaultCompactions)}
                value={draft.compactions}
                onChange={(event) => {
                  setDraft((current) => ({ ...current, compactions: event.target.value }));
                  setError(null);
                  setSaved(false);
                }}
                className="h-8 w-20 text-xs"
              />
            </div>

            <div className="flex items-center justify-between gap-3">
              <label htmlFor={remainingPctInputId} className="text-xs font-medium text-foreground">
                {t("contextLeftAtRotationLabel")}
              </label>
              <div className="flex shrink-0 items-center gap-1.5">
                <Input
                  id={remainingPctInputId}
                  type="number"
                  inputMode="numeric"
                  min={SESSION_COLLAB_BOUNDS.autoRotationRemainingPct.min}
                  max={SESSION_COLLAB_BOUNDS.autoRotationRemainingPct.max}
                  step={1}
                  aria-label={t("contextLeftAtRotationLabel")}
                  data-testid="auto-rotation-chip-remaining-pct-input"
                  disabled={saveMutation.isPending}
                  placeholder={String(defaultRemainingPct)}
                  value={draft.remainingPct}
                  onChange={(event) => {
                    setDraft((current) => ({ ...current, remainingPct: event.target.value }));
                    setError(null);
                    setSaved(false);
                  }}
                  className="h-8 w-20 text-xs"
                />
                <span className="text-xs text-muted-foreground">{t("percentUnit")}</span>
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2 pt-1">
              <Button
                type="button"
                size="sm"
                data-testid="auto-rotation-chip-save"
                data-state={saved ? "saved" : "idle"}
                disabled={saveMutation.isPending}
                onClick={handleSave}
              >
                {saveMutation.isPending ? t("savingButton") : t("saveButton")}
              </Button>
              {saved && !saveMutation.isPending && (
                <span role="status" className="text-xs text-muted-foreground">
                  {t("savedStatus")}
                </span>
              )}
            </div>
            {error && (
              <p role="alert" className="break-words text-xs text-destructive">
                {error}
              </p>
            )}
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
