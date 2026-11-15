import { MouseSensor, TouchSensor, type MouseSensorOptions, type SensorProps } from "@dnd-kit/core";

interface ChoiceSensorOptions extends MouseSensorOptions {
  activationConstraint?: { distance: number } | { delay: number; tolerance: number };
  onReady: (cancel: (() => void) | null) => void;
}
type Props = SensorProps<ChoiceSensorOptions>;

function coordinates(event: Event) {
  const point =
    event instanceof MouseEvent
      ? event
      : "touches" in event
        ? (event as TouchEvent).touches[0]
        : null;
  return point ? { x: point.clientX, y: point.clientY } : null;
}

/** Handles have no click action, so they need no document-wide click suppression. */
class ChoicePointerSensor {
  autoScrollEnabled = true;
  private started = false;
  private finished = false;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private listeners = new AbortController();
  private origin: { x: number; y: number };
  private props: Props;

  constructor(props: Props, events: { move: string; end: string; cancel?: string }) {
    this.props = props;
    const node = props.event.target as Node;
    const document = node.ownerDocument!;
    const window = document.defaultView!;
    this.origin = coordinates(props.event) ?? { x: 0, y: 0 };
    const options = { signal: this.listeners.signal };
    document.addEventListener(events.move, this.move, { ...options, passive: false });
    document.addEventListener(events.end, this.end, options);
    if (events.cancel) document.addEventListener(events.cancel, this.cancel, options);
    document.addEventListener("keydown", this.keydown, options);
    document.addEventListener("visibilitychange", this.cancel, options);
    window.addEventListener("blur", this.cancel, options);
    window.addEventListener("resize", this.cancel, options);
    document.addEventListener("dragstart", this.prevent, options);
    document.addEventListener("selectstart", this.prevent, options);
    document.addEventListener("contextmenu", this.prevent, options);
    props.options.onReady(this.cancel);
    const constraint = props.options.activationConstraint;
    if (constraint && "delay" in constraint) this.timer = setTimeout(this.start, constraint.delay);
    else if (!constraint) this.start();
  }

  private start = () => {
    if (this.finished || this.started) return;
    this.started = true;
    (this.props.event.target as Node).ownerDocument?.getSelection()?.removeAllRanges();
    this.props.onStart(this.origin);
  };

  private move = (event: Event) => {
    const point = coordinates(event);
    if (!point || this.finished) return;
    if (!this.started) {
      const distance = Math.hypot(point.x - this.origin.x, point.y - this.origin.y);
      const constraint = this.props.options.activationConstraint;
      if (constraint && "delay" in constraint) {
        if (distance > constraint.tolerance) this.cancel();
        return;
      }
      if (constraint && "distance" in constraint) {
        if (distance <= constraint.distance) return;
      }
      this.start();
    }
    if (event.cancelable) event.preventDefault();
    this.props.onMove(point);
  };

  private prevent = (event: Event) => {
    if (this.started) event.preventDefault();
  };
  private keydown = (event: KeyboardEvent) => {
    if (!this.started && (event.key === "Escape" || event.key === "Tab")) this.cancel();
  };
  private cleanup() {
    this.finished = true;
    clearTimeout(this.timer);
    this.listeners.abort();
    this.props.options.onReady(null);
  }
  private end = () => {
    if (this.finished) return;
    this.cleanup();
    if (!this.started) this.props.onAbort(this.props.active);
    this.props.onEnd();
  };
  private cancel = () => {
    if (this.finished) return;
    this.cleanup();
    if (!this.started) this.props.onAbort(this.props.active);
    this.props.onCancel();
  };
}

export class ChoiceMouseSensor extends ChoicePointerSensor {
  static activators = MouseSensor.activators;
  constructor(props: Props) {
    super(props, { move: "mousemove", end: "mouseup" });
  }
}

export class ChoiceTouchSensor extends ChoicePointerSensor {
  static activators = TouchSensor.activators;
  static setup = TouchSensor.setup;
  constructor(props: Props) {
    super(props, { move: "touchmove", end: "touchend", cancel: "touchcancel" });
  }
}
