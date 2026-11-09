// Admin-only user management (requires the admin role server-side).
import { get, patch, post } from "./client";

export interface AdminUser {
  id: string;
  username: string;
  is_active: boolean;
  roles: string[];
  created_at: string | null;
  updated_at: string | null;
}

export function listUsers(): Promise<{ users: AdminUser[] }> {
  return get<{ users: AdminUser[] }>("/api/admin/users");
}

export interface CreateUserInput {
  username: string;
  password: string;
  roles: string[];
}

export function createUser(input: CreateUserInput): Promise<AdminUser> {
  return post<AdminUser>("/api/admin/users", input);
}

export interface UpdateUserInput {
  roles?: string[];
  is_active?: boolean;
}

export function updateUser(id: string, input: UpdateUserInput): Promise<AdminUser> {
  return patch<AdminUser>(`/api/admin/users/${encodeURIComponent(id)}`, input);
}
