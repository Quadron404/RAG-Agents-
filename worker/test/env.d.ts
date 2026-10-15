import type { Env } from "../src/env";

declare module "cloudflare:test" {
  interface ProvidedEnv extends Env {
    TEST_MIGRATIONS: Parameters<typeof import("cloudflare:test")["applyD1Migrations"]>[1];
  }
}
