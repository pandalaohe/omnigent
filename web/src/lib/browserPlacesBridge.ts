export interface BrowserPlace {
  url: string;
  title: string;
  visitedAt?: number;
}
export interface PlacesResult {
  ok: boolean;
  error?: string;
  empty?: boolean;
  bookmarks?: BrowserPlace[];
  history?: BrowserPlace[];
}
interface PlacesBridge {
  browserPlaces: (args: Record<string, unknown>) => Promise<PlacesResult>;
  onBrowserPlaceChosen: (callback: (value: { viewId: string; url: string }) => void) => () => void;
}
export function browserPlacesBridge(): PlacesBridge | null {
  const bridge = (window as unknown as { omnigentDesktop?: Partial<PlacesBridge> }).omnigentDesktop;
  return typeof bridge?.browserPlaces === "function" &&
    typeof bridge.onBrowserPlaceChosen === "function"
    ? (bridge as PlacesBridge)
    : null;
}
