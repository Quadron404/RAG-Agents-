import { describe, it, expect } from "vitest";
import { sanitizeUrl } from "./markdown";

describe("markdown.sanitizeUrl", () => {
  it("keeps http and https links", () => {
    expect(sanitizeUrl("https://example.com")).toBe("https://example.com");
    expect(sanitizeUrl("http://example.com/path")).toBe("http://example.com/path");
  });
  it("keeps mailto and tel", () => {
    expect(sanitizeUrl("mailto:a@b.c")).toBe("mailto:a@b.c");
    expect(sanitizeUrl("tel:+1234567890")).toBe("tel:+1234567890");
  });
  it("neutralises javascript: URLs", () => {
    expect(sanitizeUrl("javascript:alert(1)")).toBe("#");
  });
  it("leaves non-scheme strings untouched", () => {
    expect(sanitizeUrl("JAVA SCRIPT:alert(1)")).toBe("JAVA SCRIPT:alert(1)");
    expect(sanitizeUrl("just text")).toBe("just text");
  });
  it("neutralises data: executable URLs", () => {
    expect(sanitizeUrl("data:text/html,<script>alert(1)</script>")).toBe("#");
  });
  it("neutralises vbscript:", () => {
    expect(sanitizeUrl("vbscript:msgbox(1)")).toBe("#");
  });
  it("keeps relative URLs", () => {
    expect(sanitizeUrl("/threads/1/messages")).toBe("/threads/1/messages");
    expect(sanitizeUrl("relative/path")).toBe("relative/path");
  });
  it("handles undefined", () => {
    expect(sanitizeUrl(undefined)).toBeUndefined();
  });
});
