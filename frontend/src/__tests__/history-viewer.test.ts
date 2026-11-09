import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  exportSession,
  getStepGoRunEvents,
  listHistory,
  listSessionStepGoRuns,
  listStepGoRuns,
  searchSession,
  sessionActivityAt,
  sessionStatus,
  sessionTitle,
  type HistorySession,
} from "../api/history";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ "Content-Type": "application/json" }),
    json: async () => body,
  } as unknown as Response;
}

function stubFetch(body: unknown) {
  const fetchMock = vi.fn().mockResolvedValue(jsonResponse(body));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

beforeEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("sessionStatus", () => {
  it("reports ongoing regardless of index state", () => {
    expect(sessionStatus({ is_ongoing: true, index_status: "indexed" })).toEqual({ label: "ongoing", tone: "ok" });
    expect(sessionStatus({ is_ongoing: true, index_status: "failed" })).toEqual({ label: "ongoing", tone: "ok" });
  });

  it("reports failed when indexing errored on a finished session", () => {
    expect(sessionStatus({ is_ongoing: false, index_status: "failed" })).toEqual({ label: "failed", tone: "danger" });
    expect(sessionStatus({ is_ongoing: false, index_status: "error" })).toEqual({ label: "failed", tone: "danger" });
  });

  it("reports ended otherwise", () => {
    expect(sessionStatus({ is_ongoing: false, index_status: "indexed" })).toEqual({ label: "ended", tone: "muted" });
    expect(sessionStatus({ is_ongoing: false, index_status: "pending" })).toEqual({ label: "ended", tone: "muted" });
  });

  it("accepts a full HistorySession", () => {
    const session: HistorySession = {
      id: "s",
      thread_id: "t",
      rollout_path: "/p",
      cwd: null,
      workspace_id: null,
      ai_title: null,
      model: null,
      turn_count: 0,
      total_tokens: 0,
      is_ongoing: true,
      index_status: "indexed",
      updated_at: null,
    };
    expect(sessionStatus(session).label).toBe("ongoing");
  });
});

describe("sessionTitle", () => {
  it("prefers durable title and falls back to the first user message", () => {
    expect(sessionTitle({ thread_id: "t", ai_title: "AI title", first_user_message: "first request" })).toBe("first request");
    expect(sessionTitle({ thread_id: "t", ai_title: null, first_user_message: "  first\nrequest  " })).toBe("first request");
  });

  it("uses an explicit display title before legacy fields", () => {
    expect(sessionTitle({ thread_id: "t", ai_title: "legacy", display_title: "Readable title" })).toBe("Readable title");
  });

  it("returns a stable placeholder for empty metadata", () => {
    expect(sessionTitle({ thread_id: "thread-123", ai_title: null })).toBe("Untitled session");
  });
});

describe("sessionActivityAt", () => {
  it("uses activity_at and does not fall back to index time when it is explicitly null", () => {
    expect(
      sessionActivityAt({
        activity_at: "2026-08-31T12:00:00Z",
        updated_at: "2026-08-31T11:00:00Z",
      }),
    ).toBe("2026-08-31T12:00:00Z");
    expect(sessionActivityAt({ activity_at: null, updated_at: "2026-08-31T11:00:00Z" })).toBeNull();
    expect(sessionActivityAt({ activity_at: null, updated_at: null })).toBeNull();
    expect(sessionActivityAt({ updated_at: "2026-08-31T11:00:00Z" })).toBe(
      "2026-08-31T11:00:00Z",
    );
  });
});

describe("listHistory", () => {
  it("serializes agent with search and cursor pagination", async () => {
    const fetchMock = stubFetch({ sessions: [], next_cursor: null });
    await listHistory({
      limit: 50,
      cursor: "next/page",
      q: "agent work",
      agent: "claude",
      isOngoing: false,
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/codex/sessions?limit=50&cursor=next%2Fpage&q=agent+work&agent=claude&is_ongoing=false"
    );
  });
});

describe("exportSession", () => {
  it("requests the export endpoint with the chosen format and returns content", async () => {
    const payload = { format: "markdown", filename: "session.md", content: "# transcript" };
    const fetchMock = stubFetch(payload);
    await expect(exportSession("abc", "markdown")).resolves.toEqual(payload);
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/codex/sessions/abc/export?format=markdown");
  });

  it("encodes the session id", async () => {
    const fetchMock = stubFetch({ format: "text", filename: "x.txt", content: "" });
    await exportSession("a/b c", "text");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/codex/sessions/a%2Fb%20c/export?format=text");
  });
});

describe("searchSession", () => {
  it("url-encodes the query and returns matches", async () => {
    const payload = {
      query: "foo bar",
      matches: [{ kind: "message", id: "m1", ref: "assistant", snippet: "…foo bar…" }],
    };
    const fetchMock = stubFetch(payload);
    await expect(searchSession("s1", "foo bar/baz")).resolves.toEqual(payload);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/codex/sessions/s1/search?q=foo%20bar%2Fbaz");
  });
});

describe("StepGo APIs", () => {
  it("serializes host-run filters and encodes run ids", async () => {
    const fetchMock = stubFetch({ runs: [], next_cursor: null });
    await listStepGoRuns({ limit: 25, linkStatus: "unlinked", agent: "pi" });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/codex/stepgo/runs?limit=25&link_status=unlinked&agent=pi",
    );

    fetchMock.mockResolvedValueOnce(
      jsonResponse({ events: [{ event_index: 0, raw_json: {} }], truncated: false }),
    );
    await getStepGoRunEvents("run/a b", { limit: 50 });
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/codex/stepgo/runs/run%2Fa%20b/events?limit=50",
    );

    await listSessionStepGoRuns("session/a b");
    expect(fetchMock.mock.calls[2][0]).toBe(
      "/api/codex/sessions/session%2Fa%20b/stepgo-runs",
    );
  });
});
