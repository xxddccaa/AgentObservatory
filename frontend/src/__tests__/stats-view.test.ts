import { describe, it, expect, vi, beforeEach } from "vitest";
import { getTokenUsage } from "../api/stats";

describe("token usage API", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("requests the selected range", async () => {
    const payload = {
      range: "week",
      since: "2026-08-15T00:00:00+00:00",
      until: "2026-08-22T00:00:00+00:00",
      totals: {
        input_tokens: 1,
        cached_input_tokens: 0,
        output_tokens: 2,
        reasoning_output_tokens: 0,
        total_tokens: 3,
        session_count: 1,
        turn_count: 1,
      },
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers({ "Content-Type": "application/json" }),
      json: async () => payload,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getTokenUsage("week")).resolves.toEqual(payload);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/stats/token-usage?range=week",
      expect.objectContaining({ credentials: "include" })
    );
  });
});
