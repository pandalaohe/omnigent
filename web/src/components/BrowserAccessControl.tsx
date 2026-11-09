import { useEffect, useId, useMemo, useState } from "react";
import { browserAccessBridge, type BrowserAccess } from "@/lib/nativeBridge";
import text from "@/lib/locales/browserAccess.en.json";

export function BrowserAccessControl() {
  const bridge = useMemo(browserAccessBridge, []);
  const id = useId();
  const [access, setAccess] = useState<BrowserAccess | null>(null);
  const [lists, setLists] = useState({ blocked: "", ask: "", allowed: "" });
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => {
    if (!bridge) return;
    let active = true;
    void bridge
      .get()
      .then((result) => {
        if (!active) return;
        if (!result.ok || !result.access) {
          setMessage(text.loadFailed);
          return;
        }
        setAccess(result.access);
        setLists({
          blocked: result.access.blocked.join("\n"),
          ask: result.access.ask.join("\n"),
          allowed: result.access.allowed.join("\n"),
        });
      })
      .catch(() => {
        if (active) setMessage(text.loadFailed);
      });
    return () => {
      active = false;
    };
  }, [bridge]);
  if (!bridge) return null;
  async function save() {
    if (!access || !bridge) return;
    setBusy(true);
    setMessage("");
    const entries = (value: string) =>
      value
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
    try {
      const result = await bridge.set({
        ...access,
        blocked: entries(lists.blocked),
        ask: entries(lists.ask),
        allowed: entries(lists.allowed),
      });
      setMessage(result.ok ? text.saved : (result.error ?? text.failed));
    } catch {
      setMessage(text.failed);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="space-y-3 border-t border-border pt-4" aria-label={text.title}>
      <h3 className="text-sm font-medium">{text.title}</h3>
      <p className="text-sm text-muted-foreground">{text.scope}</p>
      <label className="flex items-center justify-between gap-4 text-sm" htmlFor={`${id}-level`}>
        {text.level}
        <select
          id={`${id}-level`}
          className="rounded-md border border-input bg-background p-2"
          value={access?.level ?? "agent"}
          disabled={!access || busy}
          onChange={(event) => {
            if (access)
              setAccess({ ...access, level: event.target.value as BrowserAccess["level"] });
          }}
        >
          {(["none", "agent", "all"] as const).map((level) => (
            <option key={level} value={level}>
              {text[level]}
            </option>
          ))}
        </select>
      </label>
      <p id={`${id}-formats`} className="text-xs text-muted-foreground">
        {text.formats}
      </p>
      <div className="grid gap-3 md:grid-cols-3">
        {(["blocked", "ask", "allowed"] as const).map((key) => (
          <label key={key} className="space-y-1 text-sm" htmlFor={`${id}-${key}`}>
            <span>{text[key]}</span>
            <textarea
              id={`${id}-${key}`}
              aria-describedby={`${id}-formats`}
              className="w-full rounded-md border border-input bg-background p-2 font-mono text-xs"
              rows={4}
              value={lists[key]}
              disabled={!access || busy}
              onChange={(event) => setLists({ ...lists, [key]: event.target.value })}
            />
          </label>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          className="rounded-md border border-input px-3 py-2 text-sm disabled:opacity-50"
          disabled={!access || busy}
          onClick={() => void save()}
        >
          {text.save}
        </button>
        <span role="status" className="text-sm text-muted-foreground">
          {message || text.confirm}
        </span>
      </div>
    </section>
  );
}
