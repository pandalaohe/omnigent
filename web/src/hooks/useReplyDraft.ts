import { useCallback, useMemo, useRef, useState, type SetStateAction } from "react";
import {
  appendReplyQuote,
  restoreReplyDraft,
  removeReplyQuote,
  serializeReplyDraft,
  snapshotReplyDraft,
  type ReplyDraft,
  type StoredReplyDraft,
} from "@/lib/replyDraft";

export function useReplyDraft() {
  const [draft, setDraft] = useState<ReplyDraft>({ quotes: [], text: "" });
  const [activeTextId, setActiveTextId] = useState<string | null>(null);
  const activeTextIdRef = useRef<string | null>(null);
  // Count writes before React renders, including batched changes away and back.
  const draftRevision = useRef(0);
  const getDraftRevision = useCallback(() => draftRevision.current, []);
  const focusText = useCallback((next: SetStateAction<string | null>) => {
    const id = typeof next === "function" ? next(activeTextIdRef.current) : next;
    if (id !== activeTextIdRef.current) draftRevision.current++;
    activeTextIdRef.current = id;
    setActiveTextId(id);
  }, []);
  const value = draft.quotes.find((quote) => quote.id === activeTextId)?.before ?? draft.text;

  const editText = useCallback((id: string | null, next: SetStateAction<string>) => {
    draftRevision.current++;
    setDraft((current) => {
      const update = (text: string) => (typeof next === "function" ? next(text) : next);
      return id === null
        ? { ...current, text: update(current.text) }
        : {
            ...current,
            quotes: current.quotes.map((quote) =>
              quote.id === id ? { ...quote, before: update(quote.before) } : quote,
            ),
          };
    });
  }, []);
  const setValue = useCallback(
    (next: SetStateAction<string>) => editText(activeTextId, next),
    [activeTextId, editText],
  );
  const replaceText = useCallback(
    (text: string, saved?: StoredReplyDraft) => {
      draftRevision.current++;
      setDraft(restoreReplyDraft(text, saved));
      focusText(null);
    },
    [focusText],
  );
  const appendQuote = useCallback(
    (text: string) => {
      draftRevision.current++;
      setDraft((current) => appendReplyQuote(current, text));
      focusText(null);
    },
    [focusText],
  );
  const removeQuote = useCallback(
    (id: string) => {
      draftRevision.current++;
      setDraft((current) => removeReplyQuote(current, id));
      focusText(null);
    },
    [focusText],
  );
  const storedReplyDraft = useMemo(() => snapshotReplyDraft(draft), [draft]);

  return {
    draft,
    value,
    setValue,
    fullText: serializeReplyDraft(draft),
    storedReplyDraft,
    activeTextId,
    getDraftRevision,
    focusText,
    editText,
    replaceText,
    appendQuote,
    removeQuote,
  };
}
