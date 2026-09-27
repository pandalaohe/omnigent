import { cn } from "@/lib/utils";

/**
 * The SDK product mark: an outlined `</>` tile drawn at the Agent badge slot's
 * size (17px; 15px on a member trigger). Muted foreground so it never competes
 * with a user Agent badge or a vendor product logo, and follows the light /
 * dark theme through the token rather than a hard-coded colour.
 */
export function SdkMark({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 17 17"
      aria-hidden="true"
      data-testid="sdk-mark"
      className={cn("size-[17px] shrink-0 text-muted-foreground", className)}
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="0.75" y="0.75" width="15.5" height="15.5" rx="3.25" strokeWidth="1.5" />
      <path d="M6 5.6 3.6 8.5 6 11.4M11 5.6l2.4 2.9-2.4 2.9" strokeWidth="1.4" />
      <path d="M9.3 5.2 7.7 11.8" strokeWidth="1.2" />
    </svg>
  );
}
