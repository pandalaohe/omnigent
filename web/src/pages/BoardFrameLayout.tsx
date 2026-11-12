import { useSyncExternalStore } from "react";
import { Outlet, Link } from "@/lib/routing";
import { getFrameLoginUrl, subscribeFrameAuth } from "@/lib/identity";
import { Button } from "@/components/ui/button";

export function BoardFrameSetup() {
  return (
    <div className="p-6">
      <h1>Omnigent is not set up yet</h1>
      <Link to="/" target="_blank" rel="noopener">
        Open Omnigent
      </Link>
    </div>
  );
}

export function BoardFrameLayout() {
  const loginUrl = useSyncExternalStore(subscribeFrameAuth, getFrameLoginUrl);
  return (
    <main className="flex h-dvh min-h-0 flex-col overflow-auto bg-background text-foreground">
      {loginUrl ? (
        <section className="m-auto max-w-lg space-y-4 p-6">
          <h1 className="text-lg font-semibold">Sign in to Omnigent</h1>
          <a className="underline" href={loginUrl} target="_blank" rel="noopener">
            Sign in to Omnigent
          </a>
          <p>
            Signed in and still seeing this? This panel is on a different site than Omnigent;
            Omnigent's sign-in is only sent when both are on the same site.
          </p>
          <Button onClick={() => window.location.reload()}>Retry</Button>
        </section>
      ) : (
        <Outlet />
      )}
    </main>
  );
}
