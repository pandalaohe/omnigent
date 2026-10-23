/** Element info the in-page picker emits. All fields optional (an older script
 *  or unusual element may omit some). */
export interface DesignModeElement {
  tag?: string;
  id?: string;
  classes?: string;
  text?: string;
  testId?: string;
  ariaLabel?: string;
  role?: string;
  component?: string | null;
  selector?: string;
  pageUrl?: string;
  prefix?: string;
  suffix?: string;
}

/** Clamp per element field: element.* comes from the page DOM (UNTRUSTED), so a
 *  hostile page can't blow up the message with a huge attribute. */
const FIELD_MAX = 200;

/** Control chars + line/paragraph separators stripped from fields, so untrusted
 *  DOM text can't forge extra `[Design Mode — …]` block lines or break the `---`
 *  fences. \u escapes keep this source free of literal line terminators. */
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\u0000-\u001F\u007F-\u009F\u2028\u2029]/g;

/** Sanitize an untrusted element field: strip control chars, collapse
 *  whitespace, clamp length. Returns "" for nullish (callers treat "" as absent). */
export function sanitizeField(
  value: string | null | undefined,
  cap = FIELD_MAX,
): string {
  if (typeof value !== "string") return "";
  return value
    .replace(CONTROL_CHARS, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, cap);
}

/** Human-readable element name: prefer the React component, else the tag. Both
 *  are sanitized — they come from the page DOM. */
function displayName(el: DesignModeElement): string {
  const component = sanitizeField(el.component);
  if (component) return `<${component}>`;
  const tag = sanitizeField(el.tag);
  return `<${tag || "element"}>`;
}

/** Best-effort CSS selector for the element, in precedence order:
 *  data-testid → id → tag+classes. All parts are sanitized (untrusted DOM). */
function selectorFor(el: DesignModeElement): string {
  if (el.selector !== undefined) {
    return typeof el.selector === "string" && el.selector.length <= 700
      ? sanitizeField(el.selector, 700) || "unavailable"
      : "unavailable";
  }
  const testId = sanitizeField(el.testId);
  if (testId) return `[data-testid="${testId}"]`;
  const id = sanitizeField(el.id);
  if (id) return id; // already carries the leading '#'
  return `${sanitizeField(el.tag)}${sanitizeField(el.classes)}`;
}

/** The caller supplies the context heading; every page-derived field is bounded. */
export function formatElementEvidence(
  element: DesignModeElement,
  heading: string,
): string {
  const text = sanitizeField(element.text);
  const ariaLabel = sanitizeField(element.ariaLabel);
  const role = sanitizeField(element.role);
  const pageUrl = sanitizeField(element.pageUrl, 2000);
  const prefix = sanitizeField(element.prefix, 32);
  const suffix = sanitizeField(element.suffix, 32);
  const ctx = [
    heading,
    `Element: ${displayName(element)}`,
    `CSS selector: ${selectorFor(element)}`,
    pageUrl ? `Page: ${pageUrl}` : "",
    prefix ? `Context before: ${JSON.stringify(prefix)}` : "",
    suffix ? `Context after: ${JSON.stringify(suffix)}` : "",
    text ? `Text: "${text}"` : "",
    ariaLabel ? `Aria-label: "${ariaLabel}"` : "",
    role ? `Role: ${role}` : "",
  ]
    .filter(Boolean)
    .join("\n");
  return ctx;
}

export function buildDesignModePrompt(
  element: DesignModeElement,
  prompt: string,
): string {
  const evidence = formatElementEvidence(
    element,
    "[Design Mode — modify this element in the browser preview]",
  );
  return `${prompt}\n\n---\n${evidence}\n---`;
}
