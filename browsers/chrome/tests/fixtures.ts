import type { Activation, Metrics, Selection } from "../src/protocol";

export const activation: Activation = {
  tabId: 7,
  windowId: 3,
  documentId: "document-a",
  generation: "generation-a",
  valid: true,
};
export const metrics: Metrics = {
  width: 1000,
  height: 800,
  clientWidth: 1000,
  clientHeight: 800,
  scrollX: 0,
  scrollY: 0,
  dpr: 2,
  visual: { x: 0, y: 0, width: 1000, height: 800, scale: 1 },
  revision: 0,
  visible: true,
  url: "https://example.test/page",
};
export const selection: Selection = {
  kind: "element",
  page: {
    url: "https://example.test/page",
    title: "Demo",
    include_query_fragment: false,
  },
  target: {
    tag: "button",
    selector: "html:nth-of-type(1) > body:nth-of-type(1) > button:nth-of-type(1)",
    quote: "Save",
    prefix: "Before",
    suffix: "After",
    aria_label: "Save changes",
    role: "button",
  },
  rect: { x: 10.2, y: 20.1, width: 20.2, height: 10.2 },
  metrics,
};
