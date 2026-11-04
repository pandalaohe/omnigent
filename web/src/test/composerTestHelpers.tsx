import { forwardRef, type ComponentPropsWithoutRef, type ComponentRef } from "react";
import { MemoryRouter, useInRouterContext } from "react-router-dom";

import { Composer } from "@/pages/ChatPage";

/** Supply routing for isolated composers while preserving a caller's router. */
export const RoutedComposer = forwardRef<
  ComponentRef<typeof Composer>,
  ComponentPropsWithoutRef<typeof Composer>
>(function RoutedComposer(props, ref) {
  const inRouter = useInRouterContext();
  const composer = <Composer {...props} ref={ref} />;
  return inRouter ? composer : <MemoryRouter>{composer}</MemoryRouter>;
});
