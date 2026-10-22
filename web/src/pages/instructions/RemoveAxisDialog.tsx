import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function RemoveAxisDialog({
  name,
  layers,
  loading,
  error,
  onCancel,
  onConfirm,
}: {
  name: string;
  layers: { id: string; label: string; chars: number }[];
  loading: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !loading) onCancel();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Remove {name}?</DialogTitle>
          <DialogDescription>
            This clears the saved text below. New sessions stop receiving it; each layer's history
            is kept and can be restored.
          </DialogDescription>
        </DialogHeader>
        <ul className="space-y-2 text-sm">
          {layers.map((layer) => (
            <li key={layer.id}>
              {layer.label} · {layer.chars.toLocaleString()} chars
            </li>
          ))}
        </ul>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button variant="ghost" disabled={loading} onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="destructive" loading={loading} onClick={onConfirm}>
            Clear text and remove
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
