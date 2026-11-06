import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { refreshHostModelOptions, useHostModelOptions, type Host } from "@/hooks/useHosts";
import { copyText } from "@/lib/clipboard";
import modelGuide from "../../../../omnigent/resources/connect_models.md?raw";

export function AgentModelList({ host, harness }: { host: Host; harness: string }) {
  return <ModelListCard key={`${host.host_id}:${harness}`} host={host} harness={harness} />;
}

function ModelListCard({ host, harness }: { host: Host; harness: string }) {
  const client = useQueryClient();
  const online = host.status === "online";
  const catalog = useHostModelOptions(host.host_id, harness, online, {
    poll: false,
    timeoutMs: 35_000,
  });
  const [notice, setNotice] = useState<{ text: string; error?: boolean } | null>(null);
  const [copyNotice, setCopyNotice] = useState("");
  const refresh = useMutation({
    mutationFn: () =>
      refreshHostModelOptions(client, host.host_id, harness, "force", { timeoutMs: 35_000 }),
    onSuccess: () => setNotice({ text: "Refreshed just now." }),
    onError: () =>
      setNotice({
        text:
          catalog.data === undefined
            ? "Couldn't refresh. Try again."
            : "Couldn't refresh. Previous list kept.",
        error: true,
      }),
  });
  const rows = catalog.data ?? [];
  const source = rows[0]?.source;
  const from = source
    ? [source.label, source.name, source.host].filter(Boolean).join(" · ")
    : rows.length
      ? "Agent's own list"
      : "Not reported";
  const pending = catalog.isPending && online;

  async function copyRules() {
    try {
      await copyText(modelGuide);
      setCopyNotice("Rules copied.");
    } catch {
      setCopyNotice("Couldn't copy. Select and copy the rules below.");
    }
  }

  return (
    <Dialog
      onOpenChange={(open) => {
        if (open) setCopyNotice("");
      }}
    >
      <section aria-label="Model list" className="rounded-xl border border-border bg-card p-4">
        <h2 className="text-ui font-medium">Model list</h2>
        <dl className="mt-3 space-y-3 text-sm">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <dt className="w-12 shrink-0 text-muted-foreground">From</dt>
            <dd className="min-w-0 break-words">{pending ? "Loading…" : from}</dd>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <dt className="w-12 shrink-0 text-muted-foreground">Now</dt>
            <dd className="min-w-0">
              {pending
                ? "Loading…"
                : rows.length
                  ? `${rows.length} ${rows.length === 1 ? "model" : "models"}`
                  : "No list. Type a name in the model menu."}
            </dd>
            <Button
              size="sm"
              variant="outline"
              loading={refresh.isPending}
              disabled={!online || catalog.isFetching || refresh.isPending}
              onClick={() => {
                setNotice(null);
                refresh.mutate();
              }}
            >
              Refresh
            </Button>
            <DialogTrigger asChild>
              <Button size="sm" variant="outline">
                Connect your own
              </Button>
            </DialogTrigger>
          </div>
        </dl>
        {!online ? (
          <p className="mt-2 text-sm text-muted-foreground">Host offline.</p>
        ) : notice ? (
          <p
            role={notice.error ? "alert" : "status"}
            className={`mt-2 text-sm ${notice.error ? "text-destructive" : "text-muted-foreground"}`}
          >
            {notice.text}
          </p>
        ) : catalog.isError ? (
          <p role="alert" className="mt-2 text-sm text-destructive">
            Couldn't read the model list. Refresh to try again.
          </p>
        ) : null}
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Connect your own models</DialogTitle>
            <DialogDescription>Give these rules to the agent doing your setup.</DialogDescription>
          </DialogHeader>
          <div>
            <Button size="sm" variant="outline" onClick={() => void copyRules()}>
              Copy rules
            </Button>
            {copyNotice && (
              <p role="status" className="mt-2 text-sm">
                {copyNotice}
              </p>
            )}
          </div>
          <div className="max-h-[60vh] space-y-3 overflow-auto text-sm [&_li]:my-2 [&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:bg-muted [&_pre]:p-3 [&_ol]:list-decimal [&_ol]:pl-5 [&_ul]:list-disc [&_ul]:pl-5">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{modelGuide}</ReactMarkdown>
          </div>
        </DialogContent>
      </section>
    </Dialog>
  );
}
