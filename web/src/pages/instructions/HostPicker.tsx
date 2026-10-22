import { useState } from "react";
import { PickerShell } from "@/components/PickerShell";
import { SelectionList } from "@/components/SelectionList";
import { Button } from "@/components/ui/button";
import type { Host } from "@/hooks/useHosts";

export function HostPicker({
  hosts,
  taken,
  onPick,
}: {
  hosts: Host[];
  taken: ReadonlySet<string>;
  onPick: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <PickerShell
      open={open}
      onOpenChange={setOpen}
      title="Add host"
      trigger={<Button variant="outline">Add host</Button>}
    >
      <SelectionList
        key={String(open)}
        mode="single"
        items={hosts.map((host) => ({
          id: host.host_id,
          label: host.name,
          disabled: taken.has(host.host_id),
          aside: `${host.status}${taken.has(host.host_id) ? " · already listed" : ""}`,
        }))}
        value={null}
        onChange={(id) => {
          if (id) onPick(id);
          setOpen(false);
        }}
        searchLabel="Search hosts"
        searchThreshold={0}
        listLabel="Host choices"
        rowRole="button"
      />
    </PickerShell>
  );
}
