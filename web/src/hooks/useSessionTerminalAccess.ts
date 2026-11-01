import { useHosts } from "./useHosts";
import { useIsAdmin } from "./useIsAdmin";
import { useSession } from "./useSession";
import { isEditorLevel } from "@/lib/permissionsApi";

export function useSessionTerminalAccess(conversationId: string) {
  const { session } = useSession(conversationId);
  const { data: hosts } = useHosts();
  const isAdmin = useIsAdmin();
  // /v1/hosts is owner-scoped, including when the session itself is shared.
  const host = hosts?.find((item) => item.host_id === session?.hostId);
  return {
    session,
    host,
    canCreateElevated: Boolean(
      host?.status === "online" &&
      host.capabilities?.includes("terminal_elevated_v1") &&
      isAdmin &&
      session &&
      isEditorLevel(session.permissionLevel),
    ),
    canManageAgentAccess: host !== undefined,
  };
}
