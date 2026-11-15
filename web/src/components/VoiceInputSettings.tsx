import { useEffect, useState } from "react";
import {
  listModelConnections,
  MODEL_CONNECTIONS_CHANGED,
  type ModelConnection,
} from "@/lib/modelConnectionsApi";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  defaultVoiceInputSettings,
  defaultCleanupInstructions,
  defaultTranscriptionInstructions,
  getVoiceInputSettings,
  saveVoiceInputSettings,
} from "@/lib/voiceInputApi";

export function VoiceInputSettings() {
  const [draft, setDraft] = useState(defaultVoiceInputSettings);
  const [ready, setReady] = useState(false);
  const [notice, setNotice] = useState("");
  const [connections, setConnections] = useState<ModelConnection[]>([]);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let mounted = true;
    const refresh = () => {
      void listModelConnections()
        .then((result) => {
          if (mounted) setConnections(result.connections);
        })
        .catch(() => {});
    };
    refresh();
    window.addEventListener(MODEL_CONNECTIONS_CHANGED, refresh);
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
      window.removeEventListener(MODEL_CONNECTIONS_CHANGED, refresh);
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
      <label className="flex flex-wrap items-center gap-3 text-ui">
        Speech to text
        <select
          aria-label="Speech source"
          className="rounded border border-input bg-background p-2"
          value={draft.speech_source}
          onChange={(e) =>
            setDraft({ ...draft, speech_source: e.target.value as typeof draft.speech_source })
          }
        >
          <option value="browser">Built-in · browser first</option>
          <option value="server">Built-in · server first</option>
          <option value="third">Third-party provider</option>
        </select>
      </label>
      {draft.speech_source === "third" && (
        <>
          <label className="flex flex-wrap items-center gap-3 text-ui">
            Speech provider
            <select
              aria-label="Speech provider"
              value={draft.speech_connection}
              onChange={(e) => setDraft({ ...draft, speech_connection: e.target.value })}
            >
              <option value="">Select provider</option>
              {connections
                .filter((c) => c.editable)
                .map((c) => (
                  <option key={c.name} value={c.name}>
                    {c.display_name}
                  </option>
                ))}
            </select>
          </label>
          <label className="flex flex-col gap-2 text-ui">
            Speech model
            <input
              aria-label="Speech model"
              className="rounded border border-input bg-background p-2"
              list="voice-speech-models"
              value={draft.speech_model}
              onChange={(e) => setDraft({ ...draft, speech_model: e.target.value })}
            />
            <datalist id="voice-speech-models">
              {connections
                .find((c) => c.name === draft.speech_connection)
                ?.models.map((m) => (
                  <option key={m} value={m} />
                ))}
            </datalist>
          </label>
          <label className="flex flex-wrap items-center gap-3 text-ui">
            Speech API
            <select
              aria-label="Speech method"
              value={draft.speech_method}
              onChange={(e) =>
                setDraft({ ...draft, speech_method: e.target.value as typeof draft.speech_method })
              }
            >
              <option value="transcription">Audio transcription</option>
              <option value="chat_audio">Audio chat · transcribe and clean in one step</option>
            </select>
          </label>
          <label className="flex flex-col gap-2 text-ui">
            Transcription instructions
            <Textarea
              aria-label="Transcription instructions"
              rows={4}
              maxLength={4000}
              value={draft.speech_instructions}
              onChange={(e) => setDraft({ ...draft, speech_instructions: e.target.value })}
            />
          </label>
          <Button
            variant="outline"
            className="self-start"
            onClick={() =>
              setDraft({ ...draft, speech_instructions: defaultTranscriptionInstructions })
            }
          >
            Restore transcription default
          </Button>
        </>
      )}
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
      {draft.speech_source === "third" && draft.speech_method === "chat_audio" && (
        <p className="text-sm text-muted-foreground">
          The speech model also applies your cleanup instructions. Separate cleanup is skipped.
        </p>
      )}
      <label className="flex flex-wrap items-center gap-3 text-ui">
        Cleanup provider
        <select
          aria-label="Cleanup provider"
          value={draft.cleanup_connection}
          onChange={(e) => setDraft({ ...draft, cleanup_connection: e.target.value })}
        >
          <option value="server-llm">Server LLM (configuration file)</option>
          {connections
            .filter((c) => c.name !== "server-llm")
            .map((c) => (
              <option key={c.name} value={c.name}>
                {c.display_name}
              </option>
            ))}
        </select>
      </label>
      <datalist id="voice-cleanup-models">
        {connections
          .find((c) => c.name === draft.cleanup_connection)
          ?.models.map((m) => (
            <option key={m} value={m} />
          ))}
      </datalist>
      <details>
        <summary className="cursor-pointer text-ui">Cleanup model</summary>
        <div className="mt-3 flex flex-col gap-3">
          <label className="flex flex-col gap-2 text-ui">
            Model
            <input
              list="voice-cleanup-models"
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
