import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { MonitorIcon, MoonIcon, SunIcon } from "lucide-react";
import { PALETTES, PALETTE_TOKEN_CSS_NAMES } from "@/lib/themePalette";
import { boardDefaults, boardViews, type BoardPreferences } from "./taskBoardPreferences";
import type { TaskSource } from "./taskBoardData";
import type { UsageAccountView } from "@/pages/usageBoard/usageBoardData";
import { formatDate, t } from "./taskBoardText";
import { BoardIdentityMark, BoardProjectBullet } from "./BoardIdentityMark";
import { quotaAccounts } from "./TaskBoardQuota";

type Update = (change: Partial<BoardPreferences>) => boolean | void;
export function useBoardAppearance(preferences: BoardPreferences) {
  const [systemDark, setDark] = useState(
    () => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false,
  );
  useEffect(() => {
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!media) return;
    const change = () => setDark(media.matches);
    change();
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  const dark = preferences.theme === "dark" || (preferences.theme === "system" && systemDark);
  const tokens = (PALETTES.find((palette) => palette.id === preferences.palette) ?? PALETTES[0])
    .tokens[dark ? "dark" : "light"];
  return {
    dark,
    style: Object.fromEntries(
      Object.entries(PALETTE_TOKEN_CSS_NAMES).map(([key, name]) => [
        `--${name}`,
        tokens[key as keyof typeof tokens],
      ]),
    ) as CSSProperties,
  };
}
export function BoardAppearanceControls({
  preferences,
  update,
  part = "all",
  labelled = false,
}: {
  preferences: BoardPreferences;
  update: Update;
  part?: "all" | "theme" | "palette";
  labelled?: boolean;
}) {
  return (
    <>
      {part !== "palette" && (
        <div className="tb-segment" role="group" aria-label={t("display.theme")}>
          {(
            [
              ["system", MonitorIcon],
              ["light", SunIcon],
              ["dark", MoonIcon],
            ] as const
          ).map(([theme, Icon]) => (
            <button
              key={theme}
              type="button"
              aria-label={t(`display.${theme}`)}
              title={t(`display.${theme}`)}
              aria-pressed={preferences.theme === theme}
              onClick={() => update({ theme })}
            >
              {labelled ? t(`display.${theme}`) : <Icon size={15} />}
            </button>
          ))}
        </div>
      )}
      {part !== "theme" && (
        <select
          aria-label={t("display.palette")}
          value={preferences.palette}
          onChange={(event) =>
            update({ palette: event.target.value as BoardPreferences["palette"] })
          }
        >
          {PALETTES.map((palette) => (
            <option key={palette.id} value={palette.id}>
              {palette.label}
            </option>
          ))}
        </select>
      )}
    </>
  );
}
export function BoardClock() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  return (
    <time className="tb-clock" dateTime={new Date(now).toISOString()}>
      <b>{formatDate(now, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}</b>
      <span>{formatDate(now, { weekday: "short", month: "long", day: "numeric" })}</span>
    </time>
  );
}
function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="tb-setting-row">
      <div>
        <b>{label}</b>
        {hint && <small>{hint}</small>}
      </div>
      <div className="tb-setting-control">{children}</div>
    </div>
  );
}
export function TaskBoardDisplaySettings({
  preferences,
  update,
  storage,
  sources,
  accounts,
}: {
  preferences: BoardPreferences;
  update: Update;
  storage: string;
  sources: TaskSource[];
  accounts: UsageAccountView[];
}) {
  const pick = (
    key: "lodStation" | "lodPhase" | "boardHours" | "watch",
    options: number[],
    unit: string,
  ) => (
    <select
      aria-label={t(`display.${key}`)}
      value={preferences[key]}
      onChange={(event) => update({ [key]: Number(event.target.value) })}
    >
      {options.map((value) => (
        <option key={value} value={value}>
          {value}
          {unit}
        </option>
      ))}
    </select>
  );
  return (
    <section className="tb-display-settings" aria-label={t("display.settings")}>
      <div>
        <section>
          <h2>{t("display.opening")}</h2>
          <Row label={t("display.defaultView")} hint={t("display.nextOpen")}>
            <div className="tb-segment">
              {boardViews.map((view) => (
                <button
                  type="button"
                  key={view}
                  aria-pressed={preferences.defaultView === view}
                  onClick={() => update({ defaultView: view })}
                >
                  {t(`view.${view}`)}
                </button>
              ))}
            </div>
          </Row>
          <Row label={t("display.defaultMode")} hint={t("display.nextOpen")}>
            <div className="tb-segment">
              {(["order", "time"] as const).map((mode) => (
                <button
                  type="button"
                  key={mode}
                  aria-pressed={preferences.defaultMode === mode}
                  onClick={() => update({ defaultMode: mode })}
                >
                  {t(`routes.${mode}`)}
                </button>
              ))}
            </div>
          </Row>
        </section>
        <section>
          <h2>{t("view.routes")}</h2>
          <Row label={t("display.lodStation")} hint={t("display.stationHint")}>
            {pick("lodStation", [1, 2, 3], t("display.days"))}
          </Row>
          <Row label={t("display.lodPhase")} hint={t("display.phaseHint")}>
            {pick("lodPhase", [7, 14, 30], t("display.days"))}
          </Row>
          <Row label={t("display.boardHours")} hint={t("display.boardHint")}>
            {pick("boardHours", [12, 24, 48, 72], t("display.hours"))}
          </Row>
        </section>
        <section>
          <h2>{t("display.appearance")}</h2>
          <Row label={t("display.theme")}>
            <BoardAppearanceControls
              preferences={preferences}
              update={update}
              part="theme"
              labelled
            />
          </Row>
          <Row label={t("display.palette")}>
            <BoardAppearanceControls preferences={preferences} update={update} part="palette" />
          </Row>
          <Row label={t("display.refresh")}>
            <div className="tb-segment">
              {[30, 60, 300, 0].map((refresh) => (
                <button
                  type="button"
                  key={refresh}
                  aria-pressed={preferences.refresh === refresh}
                  onClick={() => update({ refresh })}
                >
                  {t(`display.refresh${refresh}`)}
                </button>
              ))}
            </div>
          </Row>
        </section>
      </div>
      <div>
        <section>
          <h2>{t("band.quota")}</h2>
          <Row label={t("display.pins")} hint={t("display.pinsHint")}>
            <div className="tb-setting-chips">
              {quotaAccounts(accounts, []).map((account) => {
                const pinned = preferences.pinnedQuota.includes(account.id);
                return (
                  <button
                    type="button"
                    key={account.id}
                    aria-pressed={pinned}
                    disabled={!pinned && preferences.pinnedQuota.length >= 4}
                    onClick={() =>
                      update({
                        pinnedQuota: pinned
                          ? preferences.pinnedQuota.filter((id) => id !== account.id)
                          : [...preferences.pinnedQuota, account.id],
                      })
                    }
                  >
                    <BoardIdentityMark
                      id={account.id}
                      label={account.display_name}
                      product={account.product}
                    />
                    {account.display_name}
                  </button>
                );
              })}
            </div>
          </Row>
          <Row label={t("display.watch")} hint={t("display.watchHint")}>
            {pick("watch", [60, 70, 75, 80, 85, 90], "%")}
          </Row>
        </section>
        <section>
          <h2>{t("display.hidden")}</h2>
          <Row label={t("display.hideLabel")} hint={t("display.hideHint")}>
            <div className="tb-setting-chips">
              {sources.map((source, index) => (
                <button
                  type="button"
                  key={source.id}
                  aria-pressed={preferences.hiddenSources.includes(source.id)}
                  onClick={() =>
                    update({
                      hiddenSources: preferences.hiddenSources.includes(source.id)
                        ? preferences.hiddenSources.filter((id) => id !== source.id)
                        : [...preferences.hiddenSources, source.id],
                    })
                  }
                >
                  <BoardProjectBullet
                    index={index}
                    label={preferences.aliases[source.id] || source.name || source.id}
                  />
                  {preferences.aliases[source.id] || source.name || source.id}
                </button>
              ))}
            </div>
          </Row>
        </section>
      </div>
      <footer>
        <span role="status">{t(`display.storage.${storage}`)}</span>
        <button
          type="button"
          onClick={() =>
            update({ ...boardDefaults, aliases: preferences.aliases, view: "settings" })
          }
        >
          {t("display.reset")}
        </button>
      </footer>
    </section>
  );
}
