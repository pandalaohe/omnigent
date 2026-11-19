import { boardExitProps } from "@/lib/boardFrame";
// The route entry for every Usage board page: it decides whether the module may
// render here before a page fetches anything. Deployment off is a statement;
// the user's own switch off redirects to the switch in Settings.

import { useEffect } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "@/lib/routing";

import { Spinner } from "@/components/ui/spinner";
import { usageBoardAvailable } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";

import { UsageAccountPage } from "./UsageAccountPage";
import { UsageAccountSettingsPage } from "./UsageAccountSettingsPage";
import { UsageBoardPage } from "./UsageBoardPage";
import { UsageModelsPage } from "./UsageModelsPage";
import { UsageCostPage } from "./UsageCostPage";
import { UsageIssuesPage } from "./UsageIssuesPage";

export const USAGE_BOARD_SETTINGS_PATH = "/settings/general#usage-settings";

function UsageBoardLegacyRedirect({ accountId }: { accountId?: string }) {
  const { pathname, search, hash } = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    if (accountId !== undefined) {
      const query = new URLSearchParams(search);
      query.set("account", accountId);
      navigate(
        { pathname: pathname.replace(/\/[^/]+\/?$/, ""), search: query.toString(), hash },
        { replace: true },
      );
    } else {
      navigate(
        { pathname: pathname.replace(/\/cost\/?$/, "/analysis"), search, hash },
        {
          replace: true,
          state: { keepLegacyQuery: true },
        },
      );
    }
  }, [accountId, hash, navigate, pathname, search]);
  return null;
}

export function UsageBoardRoute({
  view = "board",
}: {
  view?: "board" | "account" | "account_settings" | "models" | "cost" | "issues";
}) {
  const info = useServerInfo();
  const board = useUsageContextPreferences().board;
  const { pathname } = useLocation();
  const { accountId } = useParams();
  const [search] = useSearchParams();
  if (info === "loading") {
    return (
      <div className="flex items-center justify-center py-16">
        <Spinner className="size-5 text-muted-foreground" aria-label="Loading" />
      </div>
    );
  }
  if (!usageBoardAvailable(info)) {
    return (
      <div className="mx-auto w-full max-w-[720px] px-4 py-10 text-sm">
        <h1 className="mb-2 text-lg font-semibold">Usage board is off on this server</h1>
        <p className="text-muted-foreground">
          The server config sets <code className="text-xs">usage.enabled: false</code>, so it
          records nothing and serves no usage routes.
        </p>
      </div>
    );
  }
  if (view === "account" && accountId !== undefined) {
    return <UsageBoardLegacyRedirect accountId={accountId} />;
  }
  if (view === "cost" && /\/cost\/?$/.test(pathname)) return <UsageBoardLegacyRedirect />;
  const selected = search.get("account");
  if (
    view === "board" &&
    selected &&
    (search.has("focus") ||
      search.get("view") === "details" ||
      !board.enabled ||
      board.hidden.includes(selected))
  ) {
    return <UsageAccountPage accountId={selected} />;
  }
  if (!board.enabled && (view === "board" || view === "cost")) {
    // Not a silent redirect: the visitor is told where the switch is, and a
    // shared link still explains itself.
    return (
      <div className="mx-auto w-full max-w-[720px] px-4 py-10 text-sm">
        <h1 className="mb-2 text-lg font-semibold">Usage board is off</h1>
        <p className="mb-3 text-muted-foreground">
          It is switched off for your account. Its readings keep arriving, so turning it on shows
          them at once.
        </p>
        <Link
          {...boardExitProps()}
          className="underline underline-offset-2"
          to={USAGE_BOARD_SETTINGS_PATH}
        >
          Turn it on in Settings
        </Link>
      </div>
    );
  }
  if (view === "account_settings") return <UsageAccountSettingsPage />;
  if (view === "models") return <UsageModelsPage />;
  if (view === "cost") return <UsageCostPage />;
  if (view === "issues") return <UsageIssuesPage />;
  return <UsageBoardPage />;
}
