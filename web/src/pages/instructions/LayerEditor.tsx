import { useRef, useState } from "react";
import { MessageResponse } from "@/components/ai-elements/message";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import {
  useInstructionRevisions,
  useSaveInstructionLayer,
  type InstructionLayer,
  type InstructionRouteSize,
  type LayerKey,
} from "@/hooks/useInstructionLayers";
import { absoluteTime } from "@/lib/relativeTime";

export function LayerEditor({
  layerKey,
  layer,
  title,
  initialText,
  onClose,
}: {
  layerKey: LayerKey;
  layer?: InstructionLayer;
  title: string;
  initialText?: string;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState(initialText ?? layer?.text ?? "");
  const savedText = useRef(layer?.text ?? "");
  const [rendered, setRendered] = useState(false);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [notice, setNotice] = useState<InstructionRouteSize[] | null>(null);
  const history = useInstructionRevisions(layerKey);
  const save = useSaveInstructionLayer();
  const dirty = draft !== savedText.current;
  const close = () => {
    if (dirty) setConfirmDiscard(true);
    else onClose();
  };
  const persist = async (text: string) => {
    try {
      const result = await save.mutateAsync({ ...layerKey, text });
      savedText.current = text;
      setNotice(result.over_budget_routes);
    } catch {
      /* The mutation error remains visible next to Save. */
    }
  };
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !save.isPending) close();
      }}
    >
      <DialogContent
        className="right-0 left-auto flex w-full max-w-2xl translate-x-0 translate-y-0 flex-col overflow-y-auto rounded-none p-6 sm:max-w-2xl"
        style={{
          top: "var(--omnigent-safe-top, 0px)",
          height:
            "calc(var(--omnigent-viewport-height, 100dvh) - var(--omnigent-safe-top, 0px) - var(--omnigent-safe-bottom, 0px))",
          maxHeight:
            "calc(var(--omnigent-viewport-height, 100dvh) - var(--omnigent-safe-top, 0px) - var(--omnigent-safe-bottom, 0px))",
        }}
      >
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            {layerKey.scope === "independent"
              ? "This setting replaces Shared, Agent and Host instructions on this route. Role blocks still apply."
              : "Blank text turns this layer off. Other layers keep applying."}
          </DialogDescription>
        </DialogHeader>
        {!!layer?.stale_layers?.length && (
          <p role="status" className="text-sm text-muted-foreground">
            Replaced layers changed since this independent setting was saved:{" "}
            {layer.stale_layers.join(", ")}.
          </p>
        )}
        <Button variant="ghost" className="self-start" onClick={() => setRendered(!rendered)}>
          {rendered ? "Edit text" : "Read formatted"}
        </Button>
        {rendered ? (
          <div className="min-h-64 break-words">
            <MessageResponse>{draft || "No text in this layer."}</MessageResponse>
          </div>
        ) : (
          <Textarea
            aria-label="Instruction text"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            className="min-h-64 flex-1 font-mono text-sm leading-6"
          />
        )}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <span className="text-sm text-muted-foreground">
            {[...draft].length.toLocaleString()} characters{dirty ? " · Unsaved changes" : ""}
          </span>
          <Button
            disabled={!dirty || save.isPending}
            loading={save.isPending}
            onClick={() => void persist(draft)}
          >
            Save layer
          </Button>
        </div>
        {layerKey.scope === "independent" && layer?.text.trim() && (
          <Button
            variant="outline"
            disabled={save.isPending}
            onClick={() => {
              setDraft("");
              void persist("");
            }}
          >
            Remove independent setting
          </Button>
        )}
        {save.isError && (
          <p role="alert" className="text-sm text-destructive">
            {save.error.message}
          </p>
        )}
        {notice !== null && (
          <div role="status" className="text-sm">
            Layer saved.
            {notice.length > 0 && (
              <p>
                Newly over 10,000 characters:{" "}
                {notice.map((route) => `${route.host_id} / ${route.agent_key}`).join(", ")}. Text is
                kept in full.
              </p>
            )}
          </div>
        )}
        {confirmDiscard && (
          <div role="alert" className="rounded-md border p-3 text-sm">
            You have unsaved changes.
            <div className="mt-2 flex gap-2">
              <Button variant="outline" onClick={() => setConfirmDiscard(false)}>
                Keep editing
              </Button>
              <Button variant="destructive" onClick={onClose}>
                Discard changes
              </Button>
            </div>
          </div>
        )}
        <section className="mt-4 border-t pt-4">
          <h3 className="mb-2 font-medium">Revision history</h3>
          <p className="mb-3 text-xs text-muted-foreground">
            Load a revision into the draft, then save to restore it.
          </p>
          {history.isError && <p role="alert">{history.error.message}</p>}
          {history.data?.data.map((revision) => (
            <button
              type="button"
              key={revision.id}
              className="block w-full border-b py-3 text-left hover:bg-muted"
              onClick={() => {
                setDraft(revision.text);
                setRendered(false);
              }}
            >
              <span className="text-xs text-muted-foreground">
                {absoluteTime(revision.created_at * 1000)}
                {revision.created_by ? ` · ${revision.created_by}` : ""}
              </span>
              <span className="mt-1 line-clamp-2 whitespace-pre-wrap text-sm">
                {revision.text || "(empty)"}
              </span>
            </button>
          ))}
          {history.data?.data.length === 0 && (
            <p className="text-sm text-muted-foreground">No saved revisions yet.</p>
          )}
        </section>
      </DialogContent>
    </Dialog>
  );
}
