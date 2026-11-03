// Two lazy chunks from @tabler/icons: outline shapes load on the first Tabler
// render or picker open; search tags load only when the picker opens. The
// package exports neither JSON file, so a glob reaches the installed layout
// without making TypeScript parse two large JSON documents.

export type TablerIconNode = [tag: string, attrs: Record<string, string>][];
export type TablerIconSet = Record<string, TablerIconNode>;
export type TablerIconTags = Record<string, { category?: string; tags?: (string | number)[] }>;

const nodeFiles = import.meta.glob<TablerIconSet>(
  "/node_modules/@tabler/icons/tabler-nodes-outline.json",
  { import: "default" },
);
const tagFiles = import.meta.glob<TablerIconTags>("/node_modules/@tabler/icons/icons.json", {
  import: "default",
});

function loadOne<T>(files: Record<string, () => Promise<T>>, what: string): Promise<T> {
  const load = Object.values(files)[0];
  if (!load) {
    return Promise.reject(new Error(`@tabler/icons ${what} is missing; reinstall and rebuild`));
  }
  return load();
}

let iconSet: TablerIconSet | null = null;
let iconSetPromise: Promise<TablerIconSet> | null = null;
let iconTagsPromise: Promise<TablerIconTags> | null = null;

export function tablerIconsIfLoaded(): TablerIconSet | null {
  return iconSet;
}

/** Fetch the outline set once; a failed fetch is retried on the next call. */
export function loadTablerIcons(): Promise<TablerIconSet> {
  iconSetPromise ??= loadOne(nodeFiles, "tabler-nodes-outline.json").then(
    (set) => {
      iconSet = set;
      return set;
    },
    (error: unknown) => {
      iconSetPromise = null;
      throw error;
    },
  );
  return iconSetPromise;
}

/** Fetch search tags once; a failed fetch is retried on the next call. */
export function loadTablerIconTags(): Promise<TablerIconTags> {
  iconTagsPromise ??= loadOne(tagFiles, "icons.json").catch((error: unknown) => {
    iconTagsPromise = null;
    throw error;
  });
  return iconTagsPromise;
}
