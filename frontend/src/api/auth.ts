// Auth endpoints. Login/logout/me suppress the global 401 handler because they
// manage their own unauthenticated state (a failed login shouldn't trigger the
// "session expired → redirect" path — we're already handling it inline).
import { del, get, post, type RequestOptions } from "./client";

export interface Principal {
  id: string;
  username: string;
  roles: string[];
  bootstrap: boolean;
}

export function login(username: string, password: string): Promise<Principal> {
  return post<Principal>(
    "/api/auth/login",
    { username, password },
    { suppressAuthHandler: true }
  );
}

export function fetchMe(opts?: RequestOptions): Promise<Principal> {
  return get<Principal>("/api/auth/me", { suppressAuthHandler: true, ...opts });
}

export function logout(): Promise<{ ok: boolean }> {
  return post<{ ok: boolean }>("/api/auth/logout", undefined, {
    suppressAuthHandler: true,
  });
}

export interface CreatedToken {
  id: string;
  name: string;
  token: string;
  expires_at: string | null;
}

export function createApiToken(name: string, expiresInDays?: number): Promise<CreatedToken> {
  return post<CreatedToken>("/api/auth/tokens", {
    name,
    expires_in_days: expiresInDays ?? null,
  });
}

export interface TokenInfo {
  id: string;
  name: string;
  created_at: string | null;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
}

export function listApiTokens(): Promise<TokenInfo[]> {
  return get<TokenInfo[]>("/api/auth/tokens");
}

export function revokeApiToken(id: string): Promise<{ ok: boolean }> {
  return del<{ ok: boolean }>(`/api/auth/tokens/${encodeURIComponent(id)}`);
}

export function changePassword(currentPassword: string, newPassword: string): Promise<{ ok: boolean }> {
  return post<{ ok: boolean }>("/api/auth/password", {
    current_password: currentPassword,
    new_password: newPassword,
  });
}
