import { useLayoutEffect, useRef, useState } from "react";

/** Keep the normal font; place a reading below its circle when it cannot fit. */
export function useUsageReadingPlacement(reading: string) {
  const circleRef = useRef<HTMLSpanElement>(null);
  const readingRef = useRef<HTMLSpanElement>(null);
  const [outside, setOutside] = useState(false);

  useLayoutEffect(() => {
    const circle = circleRef.current;
    const text = readingRef.current;
    if (!circle || !text) return;
    let active = true;
    const measure = () => {
      if (!active || !circle.clientWidth) return;
      const range = document.createRange();
      range.selectNodeContents(text);
      // Sum wrapped lines too, so moving out does not make the reading fit again.
      const width = [...range.getClientRects()].reduce((sum, rect) => sum + rect.width, 0);
      setOutside(width > circle.clientWidth * 0.72);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(circle);
    observer.observe(text);
    void document.fonts?.ready.then(measure);
    return () => {
      // A font-ready callback must not remeasure the reading's previous location.
      active = false;
      observer.disconnect();
    };
  }, [reading, outside]);

  return { circleRef, readingRef, outside };
}
