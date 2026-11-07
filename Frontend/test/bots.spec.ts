import { describe, expect, it } from "vitest";
import { BOTS, BOT_BY_ID, botForRole, type BotDef } from "../src/store";

describe("Bots catalogue", () => {
  it("has four distinct bots with unique ids", () => {
    const ids = BOTS.map((b) => b.id);
    expect(ids).toHaveLength(new Set(ids).size);
    expect(ids).toContain("commander");
    expect(ids).toContain("researcher");
    expect(ids).toContain("engineer");
    expect(ids).toContain("navigator");
  });

  it("maps roles to bots", () => {
    expect(botForRole("commander").id).toBe("commander");
    expect(botForRole("chief").id).toBe("commander");
    expect(botForRole("browser").id).toBe("navigator");
    expect(botForRole("worker").id).toBe("engineer");
    expect(botForRole("engineer").id).toBe("engineer");
  });

  it("BOT_BY_ID has all bots keyed by id", () => {
    for (const bot of BOTS) {
      expect(BOT_BY_ID[bot.id]).toBe(bot);
    }
  });

  it("each bot has required fields", () => {
    for (const bot of BOTS) {
      expect(typeof bot.id).toBe("string");
      expect(typeof bot.name).toBe("string");
      expect(typeof bot.role).toBe("string");
      expect(typeof bot.blurb).toBe("string");
      expect(Array.isArray(bot.tags)).toBe(true);
      expect(typeof bot.glyph).toBe("string");
      expect(typeof bot.gradient).toBe("string");
      expect(typeof bot.color).toBe("string");
      expect(typeof bot.goal).toBe("string");
    }
  });
});
