import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { HelpTip } from "./HelpTip";

afterEach(cleanup);

describe("HelpTip", () => {
  it("shows the hint on click and hides it on Escape", async () => {
    render(<HelpTip label="About relay depth">Held until you release it.</HelpTip>);

    expect(screen.queryByText("Held until you release it.")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "About relay depth" }));
    const hint = await screen.findByText("Held until you release it.");
    expect(hint).toBeInTheDocument();

    fireEvent.keyDown(hint, { key: "Escape", code: "Escape" });
    await waitFor(() =>
      expect(screen.queryByText("Held until you release it.")).not.toBeInTheDocument(),
    );
  });
});
