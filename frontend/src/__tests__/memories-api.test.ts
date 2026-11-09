import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  exportMemories,
  getMemoryContent,
  listMemories,
} from "../api/memories";

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    headers: new Headers({ "Content-Type": "application/json" }),
    json: async () => body,
  } as unknown as Response;
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({})));
});

describe("memories API", () => {
  it("serializes list filters and pagination", async () => {
    await listMemories({
      limit: 50,
      cursor: "next/page",
      q: "deploy notes",
      agent: "claude",
      scope: "project",
      includeMissing: true,
    });
    expect(fetch).toHaveBeenCalledWith(
      "/api/memories?limit=50&cursor=next%2Fpage&q=deploy+notes&agent=claude&scope=project&include_missing=true",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("requests audited content and exports selected ids", async () => {
    await getMemoryContent("memory/one", "markdown", "copy");
    expect(fetch).toHaveBeenLastCalledWith(
      "/api/memories/memory%2Fone/content",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ format: "markdown", purpose: "copy" }),
      }),
    );
    await exportMemories(["a", "b"], "zip", "claude");
    expect(fetch).toHaveBeenLastCalledWith(
      "/api/memories/export",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ ids: ["a", "b"], format: "zip", target: "claude" }),
      }),
    );
  });
});
