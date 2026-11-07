import { describe, expect, it } from "vitest";
import { sseData } from "../src/chat/providers";

function streamFrom(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  let i = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (i < chunks.length) {
        controller.enqueue(chunks[i++]);
      } else {
        controller.close();
      }
    },
  });
}

function encode(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

describe("sseData", () => {
  it("parses a simple SSE stream with data events", async () => {
    const chunks = [
      encode("data: hello\n\n"),
      encode("data: world\n\n"),
    ];
    const values: string[] = [];
    for await (const value of sseData(streamFrom(chunks))) {
      values.push(value);
    }
    expect(values).toEqual(["hello", "world"]);
  });

  it("handles CRLF line endings", async () => {
    const chunks = [encode("data: crlf\r\n\r\n")];
    const values: string[] = [];
    for await (const value of sseData(streamFrom(chunks))) {
      values.push(value);
    }
    expect(values).toEqual(["crlf"]);
  });

  it("handles partial UTF-8 split across chunk boundaries", async () => {
    // The character "é" is UTF-8 encoded as [0xC3, 0xA9]. Split across chunks.
    const chunks = [
      encode("data: caf"),
      new Uint8Array([0xC3]),
      new Uint8Array([0xA9, 0x0a, 0x0a]),
    ];
    const values: string[] = [];
    for await (const value of sseData(streamFrom(chunks))) {
      values.push(value);
    }
    expect(values).toEqual(["café"]);
  });

  it("stops at [DONE] and ignores subsequent data", async () => {
    const chunks = [
      encode("data: first\n\n"),
      encode("data: [DONE]\n\n"),
      encode("data: after-done\n\n"),
    ];
    const values: string[] = [];
    for await (const value of sseData(streamFrom(chunks))) {
      values.push(value);
    }
    expect(values).toEqual(["first"]);
  });

  it("handles duplicate [DONE] markers", async () => {
    const chunks = [
      encode("data: first\n\n"),
      encode("data: [DONE]\n\n"),
      encode("data: [DONE]\n\n"),
    ];
    const values: string[] = [];
    for await (const value of sseData(streamFrom(chunks))) {
      values.push(value);
    }
    expect(values).toEqual(["first"]);
  });

  it("ignores empty data lines and comment lines", async () => {
    const chunks = [encode(": this is a comment\n\ndata:  \n\ndata: real\n\n")];
    const values: string[] = [];
    for await (const value of sseData(streamFrom(chunks))) {
      values.push(value);
    }
    expect(values).toEqual(["real"]);
  });

  it("handles data with space after colon", async () => {
    const chunks = [encode("data: spaced value\n\n")];
    const values: string[] = [];
    for await (const value of sseData(streamFrom(chunks))) {
      values.push(value);
    }
    expect(values).toEqual(["spaced value"]);
  });

  it("handles data without space after colon", async () => {
    const chunks = [encode("data:nospace\n\n")];
    const values: string[] = [];
    for await (const value of sseData(streamFrom(chunks))) {
      values.push(value);
    }
    expect(values).toEqual(["nospace"]);
  });

  it("yields remaining tail data after stream closes", async () => {
    const chunks = [encode("data: tail data")]; // no trailing newline
    const values: string[] = [];
    for await (const value of sseData(streamFrom(chunks))) {
      values.push(value);
    }
    expect(values).toEqual(["tail data"]);
  });

  it("handles empty stream", async () => {
    const chunks: Uint8Array[] = [];
    const values: string[] = [];
    for await (const value of sseData(streamFrom(chunks))) {
      values.push(value);
    }
    expect(values).toEqual([]);
  });
});
