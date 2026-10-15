import { defineWorkersConfig, readD1Migrations } from "@cloudflare/vitest-pool-workers/config";

export default defineWorkersConfig(async () => {
  const migrations = await readD1Migrations("migrations");
  return {
    test: {
      setupFiles: ["./test/apply-migrations.ts"],
      poolOptions: {
        workers: {
          wrangler: { configPath: "./wrangler.jsonc" },
          miniflare: {
            bindings: {
              TEST_MIGRATIONS: migrations,
              GOOGLE_CLIENT_ID: "test-client-id",
              GOOGLE_CLIENT_SECRET: "test-client-secret",
              GOOGLE_AUTH_URL: "https://accounts.google.com/o/oauth2/v2/auth",
              GOOGLE_TOKEN_URL: "https://oauth.test/token",
              GOOGLE_JWKS_URL: "https://jwks.test/certs",
              SESSION_SECRET: "test-session-secret",
              RAG_INTERNAL_TOKEN: "test-internal-token",
              BACKEND_ORIGIN: "https://backend.test",
              SESSION_TTL_SECONDS: "3600",
            },
          },
        },
      },
    },
  };
});
