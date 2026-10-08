import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { ImageContentBlock } from "@/lib/blocks";
import type { ParsedAnnotationItem } from "@/lib/annotationMessage";
import { AnnotationCards } from "./AnnotationCards";

const ITEMS: ParsedAnnotationItem[] = [
  { n: 1, label: "div.menu > button.option", body: "First note\nsecond line", imageIndex: 1 },
  { n: 2, label: "section#report > h2", body: "No image here", imageIndex: null },
];

const IMAGE: ImageContentBlock = {
  type: "input_image",
  file_id: "file-1",
  filename: "shot.png",
};

afterEach(cleanup);

describe("AnnotationCards", () => {
  it("renders one card per item with its label and body", () => {
    render(
      <AnnotationCards
        items={ITEMS}
        images={[]}
        pending={false}
        fullText="full text"
        remarkRehypeOptions={undefined}
      />,
    );

    expect(screen.getAllByTestId("annotation-card")).toHaveLength(2);
    const label = screen.getByText("div.menu > button.option");
    expect(label).toHaveAttribute("title", "div.menu > button.option");
    expect(label.tagName).toBe("CODE");
    const body = screen.getAllByTestId("annotation-body")[0]!;
    expect(body).toHaveTextContent("First note second line");
    expect(body).toHaveClass("whitespace-pre-wrap");
    expect(screen.getByText("section#report > h2")).toBeInTheDocument();
  });

  it("renders a thumbnail only for items whose image is attached", () => {
    const { container } = render(
      <AnnotationCards
        items={[
          { n: 1, label: "with image", body: "one", imageIndex: 1 },
          { n: 2, label: "no image", body: "two", imageIndex: null },
          { n: 3, label: "missing image", body: "three", imageIndex: 2 },
        ]}
        images={[IMAGE]}
        sessionId="sess-1"
        pending={false}
        fullText="full text"
        remarkRehypeOptions={undefined}
      />,
    );

    const images = container.querySelectorAll("img");
    expect(images).toHaveLength(1);
    expect(images[0]).toHaveAttribute("src", "/v1/sessions/sess-1/resources/files/file-1/content");
    expect(images[0]).toHaveAttribute("alt", "shot.png");
  });

  it("shows Sending while pending and Sent otherwise", () => {
    const { rerender } = render(
      <AnnotationCards
        items={ITEMS}
        images={[]}
        pending
        fullText="full text"
        remarkRehypeOptions={undefined}
      />,
    );
    expect(screen.getAllByText("Sending")).toHaveLength(2);
    expect(screen.queryByText("Sent")).toBeNull();

    rerender(
      <AnnotationCards
        items={ITEMS}
        images={[]}
        pending={false}
        fullText="full text"
        remarkRehypeOptions={undefined}
      />,
    );
    expect(screen.getAllByText("Sent")).toHaveLength(2);
    expect(screen.queryByText("Sending")).toBeNull();
  });

  it("discloses the full message on demand", () => {
    const fullText = [
      "Please address the following review comments.",
      "",
      "RAW_EVIDENCE_MARKER",
    ].join("\n");
    render(
      <AnnotationCards
        items={ITEMS}
        images={[]}
        pending={false}
        fullText={fullText}
        remarkRehypeOptions={undefined}
      />,
    );

    expect(screen.queryByText(/RAW_EVIDENCE_MARKER/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Show full message" }));
    expect(screen.getByText(/RAW_EVIDENCE_MARKER/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Hide full message" }));
    expect(screen.queryByText(/RAW_EVIDENCE_MARKER/)).toBeNull();
  });
});
