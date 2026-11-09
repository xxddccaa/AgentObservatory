import { describe, it, expect, vi, beforeEach } from "vitest";
import { get, post, ApiError, onUnauthorized } from "../api/client";

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ "Content-Type": "application/json" }),
    json: async () => body,
  } as unknown as Response;
}

describe("api client", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("returns parsed JSON on 2xx", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(200, { hello: "world" })));
    await expect(get("/api/x")).resolves.toEqual({ hello: "world" });
  });

  it("throws ApiError carrying status and detail on non-2xx", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(400, { detail: "bad input" })));
    await expect(get("/api/x")).rejects.toMatchObject({ status: 400, message: "bad input" });
  });

  it("preserves the public error envelope metadata", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(503, {
          error: {
            code: "service_not_ready",
            message: "Indexer is unavailable.",
            request_id: "req-123",
            retryable: true,
          },
        })
      )
    );
    await expect(get("/api/x")).rejects.toMatchObject({
      status: 503,
      code: "service_not_ready",
      message: "Indexer is unavailable.",
      requestId: "req-123",
      retryable: true,
    });
  });

  it("fires unauthorized handlers on 401", async () => {
    const handler = vi.fn();
    const off = onUnauthorized(handler);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(401, { detail: "nope" })));
    await expect(get("/api/x")).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledOnce();
    off();
  });

  it("suppresses the unauthorized handler when asked", async () => {
    const handler = vi.fn();
    const off = onUnauthorized(handler);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(401, {})));
    await expect(get("/api/x", { suppressAuthHandler: true })).rejects.toBeInstanceOf(ApiError);
    expect(handler).not.toHaveBeenCalled();
    off();
  });

  it("wraps network failures as ApiError status 0", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("boom")));
    await expect(post("/api/x", { a: 1 })).rejects.toMatchObject({ status: 0 });
  });
});
