import { describe, it, expect, vi, beforeEach } from "vitest";
import { getHealth, getLiveHealth } from "../api/health";

const components = {
  postgresql: { status: "up" },
  indexer: { status: "up" },
};

describe("getHealth", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  it("parses ok payload", async () => {
    const fetch = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({
          status: "ok",
          version: "0.1.0",
          environment: "test",
          components,
          recovery_actions: [],
        }),
      });
    vi.stubGlobal("fetch", fetch);
    const h = await getHealth();
    expect(h.status).toBe("ok");
    expect(h.components.postgresql.status).toBe("up");
    expect(h.version).toBe("0.1.0");
    expect(fetch).toHaveBeenCalledWith(
      "/api/health/ready",
      expect.objectContaining({ credentials: "include" })
    );
  });
  it("throws on non-2xx", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 503 }));
    await expect(getHealth()).rejects.toThrow();
  });

  it("uses the lightweight liveness endpoint", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: async () => ({ status: "ok", version: "0.1.0" }),
    });
    vi.stubGlobal("fetch", fetch);
    await expect(getLiveHealth()).resolves.toEqual({ status: "ok", version: "0.1.0" });
    expect(fetch).toHaveBeenCalledWith(
      "/api/health/live",
      expect.objectContaining({ credentials: "include" })
    );
  });
});
