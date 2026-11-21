import {
  fireEvent,
  getDefaultNormalizer,
  getNodeText,
  screen,
  within,
} from "@testing-library/react";
import { expect } from "vitest";

export async function expectSettingsPages(label: string, rowText: (number: number) => string) {
  const pager = within(await screen.findByRole("navigation", { name: label }));
  expect(pager.getByRole("button", { name: "Previous" })).toBeDisabled();
  const names = new Set(Array.from({ length: 45 }, (_, index) => rowText(index + 1)));
  const normalize = getDefaultNormalizer();
  const expectPage = async (page: number, start: number, count: number) => {
    await screen.findByText(rowText(start));
    expect(pager.getByText(`Page ${page}`)).toBeInTheDocument();
    expect(
      screen.queryAllByText((text) => names.has(text)).map((row) => normalize(getNodeText(row))),
    ).toEqual(Array.from({ length: count }, (_, offset) => rowText(start + offset)));
  };
  const go = (name: "Next" | "Previous") => fireEvent.click(pager.getByRole("button", { name }));
  await expectPage(1, 1, 20);
  go("Next");
  await expectPage(2, 21, 20);
  go("Next");
  await expectPage(3, 41, 5);
  expect(pager.getByRole("button", { name: "Next" })).toBeDisabled();
  return function returnToFirstPage() {
    go("Previous");
    go("Previous");
    expect(pager.getByText("Page 1")).toBeInTheDocument();
  };
}
