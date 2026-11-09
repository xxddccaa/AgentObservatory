import { beforeEach, describe, expect, it, vi } from "vitest";
import { componentEntries, getSystemLogs, getSystemStatus } from "../api/system";

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    headers: new Headers(),
    json: async () => body,
  } as unknown as Response;
}

describe("system API", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("loads the admin status contract and exposes named components", async () => {
    const payload = {
      status: "degraded",
      supervisor: { status: "running", pid: 42 },
      components: { indexer: { status: "stale", last_seen_at: "2026-08-22T12:00:00Z" } },
      recovery_actions: ["agent-observatory restart worker"],
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(payload)));
    const status = await getSystemStatus();
    expect(componentEntries(status)).toEqual([["indexer", payload.components.indexer]]);
  });

  it("bounds log pages and encodes filters and cursors", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ items: [], next_cursor: null }));
    vi.stubGlobal("fetch", fetch);
    await getSystemLogs({ component: "indexer worker", cursor: "next/item", limit: 900 });
    expect(fetch).toHaveBeenCalledWith(
      "/api/system/logs?component=indexer+worker&cursor=next%2Fitem&limit=500",
      expect.objectContaining({ credentials: "include" })
    );
  });
});
