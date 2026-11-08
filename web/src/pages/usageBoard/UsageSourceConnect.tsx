import { useState } from "react";
import { CopyIcon, PlugIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { copyText } from "@/lib/clipboard";
import { buildConnectCommand, CONNECT_SOURCES, type ConnectSource } from "./usageBoardConnect";
import { t } from "./usageText";

export function UsageSourceConnect({
  initialSource = "report",
}: {
  initialSource?: ConnectSource;
}) {
  const [source, setSource] = useState(initialSource);
  const [result, setResult] = useState<"copied" | "copyFailed" | null>(null);
  const command = buildConnectCommand(window.location.origin, source);
  return (
    <div className="flex flex-col gap-3">
      <label className="flex flex-wrap items-center gap-2 text-sm">
        {t("sourceType")}
        <select
          className="min-h-9 max-w-full rounded-md border border-border bg-background px-2"
          value={source}
          onChange={(event) => {
            setSource(event.target.value as ConnectSource);
            setResult(null);
          }}
        >
          {CONNECT_SOURCES.map((value) => (
            <option key={value} value={value}>
              {t(value)}
            </option>
          ))}
        </select>
      </label>
      <p className="text-xs leading-relaxed text-muted-foreground">{t(`${source}Help`)}</p>
      {source !== "subscription" ? (
        <>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-border bg-muted/40 p-2.5 font-mono text-[11.5px] leading-relaxed">
            {command}
          </pre>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              className="gap-1.5"
              onClick={async () => {
                try {
                  await copyText(command);
                  setResult("copied");
                } catch {
                  setResult("copyFailed");
                }
              }}
            >
              <CopyIcon className="size-3.5 shrink-0" aria-hidden="true" />
              {t(result === "copied" ? "copied" : "copy")}
            </Button>
            <span role="status" className="text-xs text-muted-foreground">
              {result ? t(result) : null}
            </span>
          </div>
          <p className="text-xs leading-relaxed text-muted-foreground">{t("credentialHelp")}</p>
        </>
      ) : null}
      <p className="text-xs leading-relaxed text-muted-foreground">{t("storageHelp")}</p>
    </div>
  );
}

export function ConnectDialog() {
  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm" className="gap-1.5">
          <PlugIcon className="size-3.5" aria-hidden="true" />
          {t("connect")}
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90dvh] max-w-[620px] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{t("connectTitle")}</DialogTitle>
          <DialogDescription>{t("connectDescription")}</DialogDescription>
        </DialogHeader>
        <UsageSourceConnect />
      </DialogContent>
    </Dialog>
  );
}
