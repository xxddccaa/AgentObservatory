// Global authentication state. `status` starts at "loading"; the router guard
// calls `bootstrap()` once on first navigation to resolve it to "authed" or
// "anon" via GET /api/auth/me. Components read getters (isAuthed, isAdmin,
// canOperate) instead of poking at roles directly.
import { defineStore } from "pinia";

import * as authApi from "../api/auth";
import type { Principal } from "../api/auth";

export type AuthStatus = "loading" | "authed" | "anon";

interface AuthState {
  principal: Principal | null;
  status: AuthStatus;
}

export const useAuthStore = defineStore("auth", {
  state: (): AuthState => ({ principal: null, status: "loading" }),
  getters: {
    isAuthed: (state): boolean => state.status === "authed" && state.principal !== null,
    roles: (state): string[] => state.principal?.roles ?? [],
    username: (state): string => state.principal?.username ?? "",
    isAdmin: (state): boolean => (state.principal?.roles ?? []).includes("admin"),
    canOperate: (state): boolean =>
      (state.principal?.roles ?? []).some((role) => role === "admin" || role === "operator"),
  },
  actions: {
    /** Resolve `loading` → `authed`/`anon` exactly once, at app start. */
    async bootstrap(): Promise<AuthStatus> {
      if (this.status !== "loading") return this.status;
      return this.refresh();
    },
    async refresh(): Promise<AuthStatus> {
      try {
        this.principal = await authApi.fetchMe();
        this.status = "authed";
      } catch {
        this.principal = null;
        this.status = "anon";
      }
      return this.status;
    },
    async login(username: string, password: string): Promise<Principal> {
      const principal = await authApi.login(username, password);
      this.principal = principal;
      this.status = "authed";
      return principal;
    },
    async logout(): Promise<void> {
      try {
        await authApi.logout();
      } catch {
        /* best-effort; clear local state regardless */
      }
      this.markAnonymous();
    },
    /** Called by the global 401 handler when a session expires mid-use. */
    markAnonymous(): void {
      this.principal = null;
      this.status = "anon";
    },
  },
});
