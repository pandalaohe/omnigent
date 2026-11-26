import type { SettingsControlProps } from "@/pages/settings/search/settingsControl";
import { Button } from "@/components/ui/button";

type SettingsPagerProps = {
  label: string;
  page: number;
  busy?: boolean;
  testIdPrefix?: string;
  previousControl?: SettingsControlProps;
  nextControl?: SettingsControlProps;
  onPrevious: () => void;
  onNext: () => void;
} & ({ pageCount: number; hasNext?: never } | { pageCount?: never; hasNext: boolean });

export function SettingsPager({
  label,
  page,
  pageCount,
  hasNext = page + 1 < (pageCount ?? 0),
  busy = false,
  testIdPrefix,
  previousControl,
  nextControl,
  onPrevious,
  onNext,
}: SettingsPagerProps) {
  if (page === 0 && !hasNext) return null;

  return (
    <nav className="flex items-center justify-center gap-3 border-t p-2" aria-label={label}>
      <Button
        type="button"
        variant="outline"
        size="sm"
        {...previousControl}
        data-testid={testIdPrefix && `${testIdPrefix}-previous`}
        disabled={page === 0 || busy}
        onClick={onPrevious}
      >
        Previous
      </Button>
      <span className="min-w-14 text-center text-sm text-muted-foreground" aria-live="polite">
        Page {page + 1}
      </span>
      <Button
        type="button"
        variant="outline"
        size="sm"
        {...nextControl}
        data-testid={testIdPrefix && `${testIdPrefix}-next`}
        disabled={!hasNext || busy}
        onClick={onNext}
      >
        Next
      </Button>
    </nav>
  );
}
