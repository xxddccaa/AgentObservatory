import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  createRetentionRun,
  formatBytes,
  getRetentionOverview,
  listRetentionRuns,
  previewRetention,
  riskLabel,
  skipReasonLabel,
} from "../api/retention";

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    headers: new Headers(),
    json: async () => body,
  } as unknown as Response;
}

describe("retention API", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("requests a fresh overview when asked", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ enabled: true, entries: [] }));
    vi.stubGlobal("fetch", fetch);
    await getRetentionOverview(true);
    expect(fetch).toHaveBeenCalledWith(
      "/api/retention/overview?refresh=true",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("sends a selection to the preview endpoint and never a path", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ totals: { bytes: 0, files: 0 } }));
    vi.stubGlobal("fetch", fetch);
    await previewRetention({ keep_days: 7, entry_ids: ["codex.sessions"], include_raw: true });
    const [path, init] = fetch.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/retention/preview");
    expect(JSON.parse(String(init.body))).toEqual({
      selection: { keep_days: 7, entry_ids: ["codex.sessions"], include_raw: true },
    });
  });

  it("posts the plan token with an explicit confirmation", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ run: { id: "ret_1" }, command_id: 7 }));
    vi.stubGlobal("fetch", fetch);
    const created = await createRetentionRun({
      plan_token: "token",
      confirm: true,
      acknowledge_large: true,
    });
    const [path, init] = fetch.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/retention/runs");
    expect(JSON.parse(String(init.body))).toEqual({
      plan_token: "token",
      confirm: true,
      acknowledge_large: true,
    });
    expect(created.command_id).toBe(7);
  });

  it("bounds run history and formats sizes and labels", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ runs: [] }));
    vi.stubGlobal("fetch", fetch);
    await listRetentionRuns(10);
    expect(fetch).toHaveBeenCalledWith(
      "/api/retention/runs?limit=10",
      expect.objectContaining({ credentials: "include" }),
    );
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(2 * 1024 ** 3)).toBe("2.0 GB");
    expect(formatBytes(15 * 1024 ** 3)).toBe("15.0 GB");
    expect(riskLabel("history")).toBe("历史会话");
    expect(skipReasonLabel("in_use")).toBe("仍在保留窗口内");
  });
});
