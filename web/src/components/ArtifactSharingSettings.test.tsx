// Tests for the General → Links card: the external-access switch and the
// write-only share code, whose PUTs replace the shown state with the server's
// reply while a failure keeps the last state.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { showToast } from "@/components/ui/toast";
import { authenticatedFetch } from "@/lib/identity";
import { ArtifactSharingSettings } from "./ArtifactSharingSettings";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));

const fetchMock = vi.mocked(authenticatedFetch);
const toastMock = vi.mocked(showToast);

function sharingResponse(
  state: { external: boolean; share_code_set: boolean; allow_comments?: boolean },
  status = 200,
) {
  return new Response(JSON.stringify(state), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  toastMock.mockReset();
});

afterEach(cleanup);

describe("ArtifactSharingSettings", () => {
  it("loads the current settings", async () => {
    fetchMock.mockResolvedValueOnce(sharingResponse({ external: true, share_code_set: true }));
    render(<ArtifactSharingSettings />);

    expect(await screen.findByRole("switch", { name: "External access" })).toBeChecked();
    expect(screen.getByRole("button", { name: "Clear" })).toBeInTheDocument();
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/artifact-sharing");
  });

  it("PUTs the switch state and shows the returned state", async () => {
    fetchMock
      .mockResolvedValueOnce(sharingResponse({ external: true, share_code_set: false }))
      .mockResolvedValueOnce(sharingResponse({ external: false, share_code_set: false }));
    render(<ArtifactSharingSettings />);

    const toggle = await screen.findByRole("switch", { name: "External access" });
    fireEvent.click(toggle);

    await waitFor(() => expect(toggle).not.toBeChecked());
    expect(fetchMock).toHaveBeenLastCalledWith("/v1/artifact-sharing", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ external: false }),
    });
  });

  it("saves the trimmed share code and clears the field", async () => {
    fetchMock
      .mockResolvedValueOnce(sharingResponse({ external: true, share_code_set: false }))
      .mockResolvedValueOnce(sharingResponse({ external: true, share_code_set: true }));
    render(<ArtifactSharingSettings />);

    const input = await screen.findByLabelText("Share code");
    fireEvent.change(input, { target: { value: "  secret-code  " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(input).toHaveValue(""));
    expect(fetchMock).toHaveBeenLastCalledWith("/v1/artifact-sharing", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ share_code: "secret-code" }),
    });
    expect(await screen.findByRole("button", { name: "Clear" })).toBeInTheDocument();
  });

  it("only enables Save for a 4–64 character code", async () => {
    fetchMock.mockResolvedValueOnce(sharingResponse({ external: true, share_code_set: false }));
    render(<ArtifactSharingSettings />);

    const input = await screen.findByLabelText("Share code");
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();

    fireEvent.change(input, { target: { value: "abc" } });
    expect(save).toBeDisabled();

    fireEvent.change(input, { target: { value: "abcd" } });
    expect(save).toBeEnabled();
  });

  it("offers Clear only when a code is set and clears it", async () => {
    fetchMock
      .mockResolvedValueOnce(sharingResponse({ external: true, share_code_set: false }))
      .mockResolvedValueOnce(sharingResponse({ external: true, share_code_set: true }))
      .mockResolvedValueOnce(sharingResponse({ external: true, share_code_set: false }));
    render(<ArtifactSharingSettings />);

    await screen.findByRole("switch", { name: "External access" });
    expect(screen.queryByRole("button", { name: "Clear" })).toBeNull();

    const input = screen.getByLabelText("Share code");
    fireEvent.change(input, { target: { value: "abcd" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    fireEvent.click(await screen.findByRole("button", { name: "Clear" }));

    await waitFor(() => expect(screen.queryByRole("button", { name: "Clear" })).toBeNull());
    expect(fetchMock).toHaveBeenLastCalledWith("/v1/artifact-sharing", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ share_code: null }),
    });
  });

  it("toggles visitor comments with an allow_comments-only PUT", async () => {
    fetchMock
      .mockResolvedValueOnce(
        sharingResponse({ external: true, share_code_set: false, allow_comments: true }),
      )
      .mockResolvedValueOnce(
        sharingResponse({ external: true, share_code_set: false, allow_comments: false }),
      );
    render(<ArtifactSharingSettings />);

    const toggle = await screen.findByRole("switch", { name: "Visitor comments" });
    expect(toggle).toBeChecked();
    fireEvent.click(toggle);

    await waitFor(() => expect(toggle).not.toBeChecked());
    // Only the changed field may travel: the server keeps the gate key when
    // a write touches allow_comments alone, so the switch never signs a
    // visitor out.
    expect(fetchMock).toHaveBeenLastCalledWith("/v1/artifact-sharing", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allow_comments: false }),
    });
  });

  it("keeps the previous switch state when a save fails", async () => {
    fetchMock
      .mockResolvedValueOnce(sharingResponse({ external: true, share_code_set: false }))
      .mockResolvedValueOnce(sharingResponse({ external: false, share_code_set: false }, 400));
    render(<ArtifactSharingSettings />);

    const toggle = await screen.findByRole("switch", { name: "External access" });
    fireEvent.click(toggle);

    await waitFor(() =>
      expect(toastMock).toHaveBeenCalledWith("Couldn't save link settings.", { duration: 0 }),
    );
    expect(toggle).toBeChecked();
  });

  it("retries a failed initial load instead of leaving the card empty", async () => {
    fetchMock
      .mockResolvedValueOnce(sharingResponse({ external: true, share_code_set: true }, 503))
      .mockResolvedValueOnce(sharingResponse({ external: true, share_code_set: true }));
    render(<ArtifactSharingSettings />);

    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("switch", { name: "External access" })).toBeChecked();
    expect(screen.getByRole("button", { name: "Clear" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
