import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  defaultVoiceInputSettings,
  defaultCleanupInstructions,
  getVoiceInputSettings,
  saveVoiceInputSettings,
} from "@/lib/voiceInputApi";

export function VoiceInputSettings() {
  const [draft, setDraft] = useState(defaultVoiceInputSettings);
  const [ready, setReady] = useState(false);
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let mounted = true;
    void getVoiceInputSettings()
      .then((settings) => {
        if (!mounted) return;
        setDraft(settings);
        setReady(true);
      })
      .catch(() => {
        if (mounted) setNotice("Couldn't load voice settings. Reopen settings to retry.");
      });
    return () => {
      mounted = false;
    };
  }, []);

  const save = async () => {
    setSaving(true);
    try {
      const value = await saveVoiceInputSettings(draft);
      setDraft(value);
      setNotice("Voice settings saved");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Couldn't save voice settings");
    } finally {
      setSaving(false);
    }
  };

  return (
    <fieldset disabled={!ready || saving} className="flex min-w-0 flex-col gap-3">
      <label className="flex flex-col gap-2 text-ui">
        Word list
        <span className="text-sm text-muted-foreground">
          One term per line. Add alternatives with “may be heard as”. Saved across your devices.
        </span>
        <Textarea
          value={draft.words}
          maxLength={8000}
          rows={4}
          aria-label="Voice word list"
          placeholder="ExampleApp may be heard as example app"
          onChange={(event) => setDraft({ ...draft, words: event.target.value })}
        />
      </label>
      <label className="flex flex-wrap items-center gap-3 text-ui">
        Cleanup
        <select
          aria-label="Voice cleanup mode"
          className="rounded border border-input bg-background p-2"
          value={draft.mode}
          onChange={(event) =>
            setDraft({ ...draft, mode: event.target.value as typeof draft.mode })
          }
        >
          <option value="automatic">On</option>
          <option value="off">Off</option>
        </select>
      </label>
      <p className="text-sm text-muted-foreground">
        Only newly dictated text and your word list go to the configured text provider. Restore the
        original with your device’s Undo command where supported.
      </p>
      <details>
        <summary className="cursor-pointer text-ui">Cleanup model</summary>
        <div className="mt-3 flex flex-col gap-3">
          <label className="flex flex-col gap-2 text-ui">
            Model
            <input
              aria-label="Voice cleanup model"
              className="rounded border border-input bg-background p-2"
              value={draft.model}
              placeholder="Leave empty to turn cleanup off"
              maxLength={200}
              onChange={(event) => setDraft({ ...draft, model: event.target.value })}
            />
          </label>
          <label className="flex flex-wrap items-center gap-3 text-ui">
            Timeout (seconds)
            <input
              type="number"
              min={1}
              max={60}
              aria-label="Voice cleanup timeout"
              value={draft.timeout}
              className="w-24 rounded border border-input bg-background p-2"
              onChange={(event) => setDraft({ ...draft, timeout: Number(event.target.value) })}
            />
          </label>
        </div>
      </details>
      <label className="flex flex-col gap-2 text-ui">
        Cleanup instructions
        <Textarea
          aria-label="Voice cleanup instructions"
          rows={8}
          maxLength={4000}
          value={draft.instructions}
          onChange={(event) => setDraft({ ...draft, instructions: event.target.value })}
        />
      </label>
      <Button
        type="button"
        variant="outline"
        className="self-start"
        onClick={() => setDraft({ ...draft, instructions: defaultCleanupInstructions })}
      >
        Restore default
      </Button>
      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" onClick={() => void save()} disabled={!ready || saving}>
          {saving ? "Saving…" : "Save voice settings"}
        </Button>
        <span role="status" className="text-sm text-muted-foreground">
          {notice}
        </span>
      </div>
    </fieldset>
  );
}
