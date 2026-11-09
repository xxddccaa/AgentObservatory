import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  getProject,
  getProjectTimeseries,
  listProjectSessions,
  listProjects,
} from "../api/projects";

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    headers: new Headers({ "Content-Type": "application/json" }),
    json: async () => body,
  } as unknown as Response;
}

describe("projects API", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("serializes search, range, pagination, and optional filters", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ items: [], total: 0, range: "week" }));
    vi.stubGlobal("fetch", fetchMock);

    await listProjects({
      limit: 50,
      offset: 10,
      q: "/tmp/demo project",
      range: "week",
      agent: "step code",
      model: "gpt/test",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/projects?limit=50&offset=10&q=%2Ftmp%2Fdemo+project&range=week&agent=step+code&model=gpt%2Ftest",
      expect.objectContaining({ credentials: "include" })
    );
  });

  it("encodes workspace ids for detail and trend requests", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response({ project: {} }))
      .mockResolvedValueOnce(response({ points: [] }))
      .mockResolvedValueOnce(response({ sessions: [], next_cursor: null }));
    vi.stubGlobal("fetch", fetchMock);

    await getProject("workspace/a b");
    await getProjectTimeseries("workspace/a b", "month");
    await listProjectSessions("workspace/a b", { limit: 100, cursor: "next/page" });

    expect(fetchMock.mock.calls[0][0]).toBe("/api/projects/workspace%2Fa%20b");
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/projects/workspace%2Fa%20b/timeseries?range=month"
    );
    expect(fetchMock.mock.calls[2][0]).toBe(
      "/api/codex/sessions?workspace_id=workspace%2Fa+b&limit=100&cursor=next%2Fpage"
    );
  });
});
