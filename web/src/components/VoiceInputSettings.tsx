import { useEffect, useId, useState } from "react";
import {
  listModelConnections,
  MODEL_CONNECTIONS_CHANGED,
  type ModelConnection,
} from "@/lib/modelConnectionsApi";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  defaultVoiceInputSettings,
  defaultCleanupInstructions,
  defaultTranscriptionInstructions,
  getVoiceInputSettings,
  saveVoiceInputSettings,
} from "@/lib/voiceInputApi";

const rowClass = "grid min-w-0 grid-cols-[7rem_minmax(0,1fr)] items-center gap-x-3 gap-y-2 text-ui";
const controlClass = "w-full min-w-0 max-w-64";

function SettingsSelect({
  label,
  name = label,
  value,
  onChange,
  options,
  disabled = false,
}: {
  label: string;
  name?: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string; disabled?: boolean }[];
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className={rowClass}>
      <label htmlFor={id}>{label}</label>
      <Select
        value={value || "__empty"}
        onValueChange={(selectedValue) =>
          onChange(selectedValue === "__empty" ? "" : selectedValue)
        }
        disabled={disabled}
      >
        <SelectTrigger id={id} aria-label={name} className={controlClass}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent position="popper" align="start">
          {options.map((option) => (
            <SelectItem
              key={option.value || "__empty"}
              value={option.value || "__empty"}
              disabled={option.disabled}
            >
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

function ModelPicker({
  label,
  name,
  value,
  models,
  onChange,
  disabled,
}: {
  label: string;
  name: string;
  value: string;
  models: string[];
  onChange: (value: string) => void;
  disabled: boolean;
}) {
  const [custom, setCustom] = useState(false);
  const known = [...new Set(models)];
  const index = known.indexOf(value);
  const useCustom = custom || (!!value && index === -1);
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <SettingsSelect
        label={label}
        name={name}
        disabled={disabled}
        value={useCustom ? "custom" : index >= 0 ? `model-${index}` : "none"}
        options={[
          { value: "none", label: "Select model" },
          ...known.map((model, i) => ({ value: `model-${i}`, label: model })),
          { value: "custom", label: "Custom model ID…" },
        ]}
        onChange={(selected) => {
          setCustom(selected === "custom");
          if (selected !== "custom")
            onChange(selected === "none" ? "" : known[Number(selected.slice(6))]);
        }}
      />
      {useCustom && (
        <label className={rowClass}>
          Custom model ID
          <input
            className={`${controlClass} rounded-lg border border-input bg-background px-2.5 py-2`}
            aria-label={`${label} custom model ID`}
            disabled={disabled}
            value={value}
            maxLength={200}
            onChange={(event) => onChange(event.target.value)}
          />
        </label>
      )}
    </div>
  );
}

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

  const speechProvider = connections.find(
    (connection) => connection.name === draft.speech_connection,
  );
  const audioChatAvailable = speechProvider?.wire_api === "chat";
  const oneStep = draft.speech_source === "third" && draft.speech_method === "chat_audio";
  const disabled = !ready || saving;

  return (
    <fieldset disabled={disabled} className="flex min-w-0 flex-col gap-3">
      <div role="group" aria-label="Speech to text" className="flex min-w-0 flex-col gap-3">
        <SettingsSelect
          label="Speech to text"
          name="Speech source"
          value={draft.speech_source}
          disabled={disabled}
          options={[
            { value: "browser", label: "Built-in · browser first" },
            { value: "server", label: "Built-in · server first" },
            { value: "third", label: "Third-party provider" },
          ]}
          onChange={(value) =>
            setDraft({ ...draft, speech_source: value as typeof draft.speech_source })
          }
        />
        {draft.speech_source === "third" && (
          <>
            <SettingsSelect
              label="Speech provider"
              value={draft.speech_connection}
              disabled={disabled}
              options={[
                { value: "", label: "Select provider" },
                ...connections
                  .filter((connection) => connection.editable)
                  .map((connection) => ({
                    value: connection.name,
                    label: connection.display_name,
                  })),
              ]}
              onChange={(value) =>
                setDraft({
                  ...draft,
                  speech_connection: value,
                  speech_method:
                    connections.find((connection) => connection.name === value)?.wire_api === "chat"
                      ? draft.speech_method
                      : "transcription",
                })
              }
            />
            <ModelPicker
              key={`speech-${draft.speech_connection}`}
              label="Speech model"
              name="Speech model"
              value={draft.speech_model}
              models={speechProvider?.models ?? []}
              disabled={disabled}
              onChange={(value) => setDraft({ ...draft, speech_model: value })}
            />
            <SettingsSelect
              label="Speech API"
              name="Speech method"
              value={draft.speech_method}
              disabled={disabled}
              options={[
                { value: "transcription", label: "Audio transcription" },
                ...(audioChatAvailable
                  ? [{ value: "chat_audio", label: "Audio chat + cleanup" }]
                  : []),
              ]}
              onChange={(value) =>
                setDraft({ ...draft, speech_method: value as typeof draft.speech_method })
              }
            />
            {audioChatAvailable && (
              <label className="flex min-w-0 items-center justify-between gap-3 text-ui">
                <span>
                  Also clean up in the same request
                  <span className="block text-sm text-muted-foreground">
                    Requires a model that accepts audio chat.
                  </span>
                </span>
                <Switch
                  aria-label="Also clean up in the same request"
                  checked={oneStep}
                  disabled={disabled}
                  onCheckedChange={(checked) =>
                    setDraft({ ...draft, speech_method: checked ? "chat_audio" : "transcription" })
                  }
                />
              </label>
            )}
            <label className="flex flex-col gap-2 text-ui">
              Transcription instructions
              <Textarea
                aria-label="Transcription instructions"
                rows={4}
                maxLength={4000}
                value={draft.speech_instructions}
                onChange={(event) =>
                  setDraft({ ...draft, speech_instructions: event.target.value })
                }
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
      </div>
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
      <div className={rowClass}>
        <span>
          Cleanup
          {oneStep && (
            <span className="block text-sm text-muted-foreground">
              Skipped: speech also cleans up.
            </span>
          )}
        </span>
        <Select
          value={draft.mode === "off" ? "off" : "automatic"}
          disabled={disabled || oneStep}
          onValueChange={(value) => setDraft({ ...draft, mode: value as typeof draft.mode })}
        >
          <SelectTrigger aria-label="Voice cleanup mode" className={controlClass}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper" align="start">
            <SelectItem value="automatic">On</SelectItem>
            <SelectItem value="off">Off</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <p className="text-sm text-muted-foreground">
        Only newly dictated text and your word list go to the configured text provider. Restore the
        original with your device’s Undo command where supported.
      </p>
      <SettingsSelect
        label="Cleanup provider"
        value={draft.cleanup_connection}
        disabled={disabled || oneStep}
        options={[
          { value: "server-llm", label: "Server LLM (configuration file)" },
          ...connections
            .filter((connection) => connection.name !== "server-llm")
            .map((connection) => ({ value: connection.name, label: connection.display_name })),
        ]}
        onChange={(value) => setDraft({ ...draft, cleanup_connection: value })}
      />
      <ModelPicker
        key={`cleanup-${draft.cleanup_connection}`}
        label="Cleanup model"
        name="Voice cleanup model"
        value={draft.model}
        models={
          connections.find((connection) => connection.name === draft.cleanup_connection)?.models ??
          []
        }
        disabled={disabled || oneStep}
        onChange={(value) => setDraft({ ...draft, model: value })}
      />
      <label className={rowClass}>
        Timeout (seconds)
        <input
          type="number"
          min={1}
          max={60}
          aria-label="Voice cleanup timeout"
          value={draft.timeout}
          className="w-24 rounded-lg border border-input bg-background px-2.5 py-2"
          disabled={disabled || oneStep}
          onChange={(event) => setDraft({ ...draft, timeout: Number(event.target.value) })}
        />
      </label>
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
