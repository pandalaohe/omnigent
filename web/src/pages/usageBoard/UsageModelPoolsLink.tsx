import { useExtensions } from "@/extensions/ExtensionProvider";
import { Link } from "@/lib/routing";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import { patchUsageBoardPreferences } from "@/lib/usageContextPreferences";

export function UsageModelPoolsLink({ settings = false }: { settings?: boolean }) {
  const selected = useUsageContextPreferences().board.modelPoolsPage;
  const pages = useExtensions().flatMap((extension) =>
    extension.pages.map((page) => ({
      path: `/extensions/${extension.id}/${page.route}`,
      title: `${extension.display_name} · ${page.title}`,
    })),
  );
  const page = pages.find((item) => item.path === selected);
  if (settings)
    return (
      <label className="flex min-h-12 flex-wrap items-center justify-between gap-2 border-t border-border py-2 text-sm">
        Model pools page
        <select
          aria-label="Model pools page"
          className="min-h-11 min-w-0 max-w-full truncate rounded border border-border bg-background px-2"
          title={page?.title ?? "Not configured"}
          value={page?.path ?? ""}
          onChange={(event) => patchUsageBoardPreferences({ modelPoolsPage: event.target.value })}
        >
          <option value="">Not configured</option>
          {pages.map((item) => (
            <option key={item.path} value={item.path} title={item.title}>
              {item.title}
            </option>
          ))}
        </select>
      </label>
    );
  return page ? (
    <Link
      className="inline-flex min-h-11 items-center text-sm underline underline-offset-2"
      to={page.path}
    >
      Model pools ↗
    </Link>
  ) : (
    <p className="text-xs text-muted-foreground">
      Model pools · Not available yet. Choose an installed page in Usage board settings.
    </p>
  );
}
