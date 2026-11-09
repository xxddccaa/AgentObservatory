import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  getInsightEvidence,
  getInsightsExportUrl,
  getInsightsOverview,
  getSessionTimeline,
  listInsightEvidence,
} from "../api/insights";

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    headers: new Headers({ "Content-Type": "application/json" }),
    json: async () => body,
  } as unknown as Response;
}

beforeEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Insights API", () => {
  it("encodes aggregate filters without sending custom as a backend range", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      response({
        since: "2026-08-01T00:00:00Z",
        until: "2026-08-02T00:00:00Z",
        totals: {},
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await getInsightsOverview({
      range: "custom",
      since: "2026-08-01T00:00:00Z",
      until: "2026-08-02T00:00:00Z",
      workspace: "/tmp/a b",
      agent: "stepcode",
      confidence: "derived",
    });

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/insights/overview?from=2026-08-01T00%3A00%3A00Z&to=2026-08-02T00%3A00%3A00Z&workspace_id=%2Ftmp%2Fa+b&agent=stepcode"
    );
    expect(fetchMock.mock.calls[0][1]).toEqual(expect.objectContaining({ credentials: "include" }));
  });

  it("requests encoded timeline and evidence identifiers", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ session_id: "a/b", spans: [] }))
      .mockResolvedValueOnce(response({ id: "ev/1" }))
      .mockResolvedValueOnce(response({ items: [] }));
    vi.stubGlobal("fetch", fetchMock);

    await getSessionTimeline("a/b");
    await getInsightEvidence("ev/1");
    await listInsightEvidence("source/1", {
      range: "custom",
      since: "2026-08-01T00:00:00Z",
      until: "2026-08-02T00:00:00Z",
    });

    expect(fetchMock.mock.calls[0][0]).toBe("/api/insights/sessions/a%2Fb/timeline");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/insights/evidence/ev%2F1");
    expect(fetchMock.mock.calls[2][0]).toBe(
      "/api/insights/evidence?from=2026-08-01T00%3A00%3A00Z&to=2026-08-02T00%3A00%3A00Z&source_id=source%2F1&limit=100"
    );
  });

  it("builds auditable export URLs with the active filters", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-25T12:00:00.000Z"));
    expect(getInsightsExportUrl("csv", { range: "30d", status: "failed", agent: "claude" })).toBe(
      "/api/insights/export?format=csv&from=2026-07-26T12%3A00%3A00.000Z&to=2026-08-25T12%3A00%3A00.000Z&agent=claude"
    );
  });
});
