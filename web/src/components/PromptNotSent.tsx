import { HelpTip } from "@/components/HelpTip";

export function PromptNotSent({ name }: { name: string }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
      Not sent · builds its own prompt
      <HelpTip label={`Why startup prompts do not reach ${name}`}>
        {name} builds its own prompt on the machine it runs on. Startup prompts don't reach it.
        Anything it should always know is set there. To tell it something for one chat, say it in
        your first message.
      </HelpTip>
    </span>
  );
}
