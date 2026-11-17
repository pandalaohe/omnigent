// The Changes toolbar's segmented control: the PR's changed files or its commits.

import { Segmented } from "@/components/ui/segmented";

export type ChangesSource = "changes" | "commits";

const OPTIONS: { value: ChangesSource; label: string }[] = [
  { value: "changes", label: "Changes" },
  { value: "commits", label: "Commits" },
];

export function PullRequestChangesModeSwitch({
  value,
  onChange,
}: {
  value: ChangesSource;
  onChange: (value: ChangesSource) => void;
}) {
  return (
    <Segmented
      label="Show changes or commits"
      options={OPTIONS}
      value={value}
      onChange={onChange}
    />
  );
}
