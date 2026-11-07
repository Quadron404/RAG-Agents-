import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Markdown } from "../src/lib/markdown";

describe("Markdown", () => {
  it("renders plain text", () => {
    render(<Markdown text="Hello world" />);
    expect(screen.getByText("Hello world")).not.toBeNull();
  });

  it("renders bold text", () => {
    render(<Markdown text="**bold**" />);
    expect(screen.getByText("bold")).not.toBeNull();
  });

  it("renders streaming text with trailing space", () => {
    render(<Markdown text="streaming" streaming />);
    expect(screen.getByText(/streaming/)).not.toBeNull();
  });

  it("renders empty text without crashing", () => {
    const { container } = render(<Markdown text="" />);
    expect(container.querySelector(".md")).not.toBeNull();
  });
});
