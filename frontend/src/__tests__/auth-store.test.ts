import { describe, it, expect, vi, beforeEach } from "vitest";
import { setActivePinia, createPinia } from "pinia";
import { useAuthStore } from "../stores/auth";

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers({ "Content-Type": "application/json" }),
    json: async () => body,
  } as unknown as Response;
}

describe("auth store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it("bootstrap resolves to authed when /me succeeds", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(200, { id: "1", username: "admin", roles: ["admin", "operator"], bootstrap: false })
      )
    );
    const auth = useAuthStore();
    await auth.bootstrap();
    expect(auth.status).toBe("authed");
    expect(auth.isAuthed).toBe(true);
    expect(auth.isAdmin).toBe(true);
    expect(auth.canOperate).toBe(true);
    expect(auth.username).toBe("admin");
  });

  it("bootstrap resolves to anon when /me returns 401", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(401, { detail: "nope" })));
    const auth = useAuthStore();
    await auth.bootstrap();
    expect(auth.status).toBe("anon");
    expect(auth.isAuthed).toBe(false);
  });

  it("login authenticates and logout clears state", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(200, { id: "2", username: "viewer", roles: ["viewer"], bootstrap: false })
      )
      .mockResolvedValueOnce(jsonResponse(200, { ok: true }));
    vi.stubGlobal("fetch", fetchMock);
    const auth = useAuthStore();

    await auth.login("viewer", "viewerpass123");
    expect(auth.isAuthed).toBe(true);
    expect(auth.isAdmin).toBe(false);
    expect(auth.canOperate).toBe(false);

    await auth.logout();
    expect(auth.status).toBe("anon");
    expect(auth.principal).toBeNull();
  });

  it("markAnonymous resets state (used by the global 401 handler)", () => {
    const auth = useAuthStore();
    auth.$patch({ principal: { id: "3", username: "x", roles: ["viewer"], bootstrap: false }, status: "authed" });
    auth.markAnonymous();
    expect(auth.status).toBe("anon");
    expect(auth.principal).toBeNull();
  });
});
