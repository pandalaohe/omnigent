import { useState } from "react";
import { CopyIcon, PlugIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
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
        <Combobox
          mode="select"
          options={CONNECT_SOURCES.map((value) => ({ key: value, label: t(value) }))}
          value={source}
          onValueChange={(value) => {
            setSource(value as ConnectSource);
            setResult(null);
          }}
          aria-label={t("sourceType")}
          className="h-auto w-56 max-w-full rounded-none! bg-transparent p-0"
        />
      </label>
      <p className="text-xs leading-relaxed text-muted-foreground">{t(`${source}Help`)}</p>
      {source !== "subscription" ? (
        <>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-border bg-muted/40 p-2.5 font-mono text-[11.5px] leading-relaxed">
            {command}
          </pre>
          <div className="flex flex-wrap items-center gap-2">
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    variant="outline"
                    size="icon-sm"
                    aria-label={t("copy")}
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
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{t(result === "copied" ? "copied" : "copy")}</TooltipContent>
              </Tooltip>
            </TooltipProvider>
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
