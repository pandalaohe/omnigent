import { Textarea } from "@/components/ui/textarea";
import { PromptNotSent } from "@/components/PromptNotSent";

export function SessionPromptField({
  value,
  onChange,
  name,
  injectsPrompt,
}: {
  value: string;
  onChange: (value: string) => void;
  name: string;
  injectsPrompt?: boolean | null;
}) {
  if (injectsPrompt === false) return <PromptNotSent name={name} />;
  const bytes = new TextEncoder().encode(value).length;
  return (
    <details className="min-w-0 text-sm" open={value ? true : undefined}>
      <summary className="cursor-pointer text-muted-foreground">Prompt for this chat</summary>
      <label className="mt-2 block space-y-1.5">
        <span className="sr-only">Prompt for this chat</span>
        <Textarea
          value={value}
          onChange={(event) => onChange(event.target.value)}
          maxLength={16384}
          rows={2}
          placeholder="Nothing added"
          aria-invalid={bytes > 16384}
          className="min-h-16"
        />
      </label>
      <p className="mt-1 text-xs text-muted-foreground">
        Optional · Added after Startup prompts · {bytes.toLocaleString()} / 16,384 bytes
      </p>
      {bytes > 16384 && (
        <p role="alert" className="text-xs text-destructive">
          Shorten the prompt to 16 KiB or less.
        </p>
      )}
    </details>
  );
}
