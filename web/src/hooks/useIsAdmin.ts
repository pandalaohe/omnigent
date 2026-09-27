import { useQuery } from "@tanstack/react-query";
import { getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";

// Mode-agnostic admin gate, sourced from the `/v1/me` identity probe
// (the shared `users.is_admin` column). Unlike `useMe`, which reads the
// accounts-only `/auth/me` endpoint, this works in EVERY auth mode —
// header, accounts, AND OIDC/SSO — so admin chrome (the Members /
// Policies settings sections) can surface under OIDC where `/auth/me`
// doesn't exist.
const QUERY_KEY = ["identity-is-admin"];

/**
 * Whether the current user is an admin, per `GET /v1/me`. Returns false
 * until identity resolves. Cached briefly so gating is instant across
 * consumers without re-probing on every navigation. Server enforces the
 * flag on every admin route regardless — this is chrome only.
 */
export function useIsAdmin(): boolean {
  const { data } = useQuery<boolean>({
    queryKey: QUERY_KEY,
    queryFn: async () => {
      await resolveIdentity();
      return getCurrentIsAdmin();
    },
    staleTime: 30_000,
    // Placeholder, not initialData: the app mounts before GET /v1/me
    // resolves, and a seeded initialData value is stamped fresh for
    // `staleTime`, so the queryFn would never run on that first load and
    // the admin group would stay hidden until some later invalidation.
    // A placeholder is uncached and does not count as fresh, so the
    // query still fetches on mount, while first paint stays correct when
    // identity already resolved during boot (the common case).
    placeholderData: getCurrentIsAdmin,
  });
  return data ?? false;
}
