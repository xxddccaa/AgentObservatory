import { ApiError } from "./client";
import type { ReadyHealth } from "./health";

export interface ActionableError {
  title: string;
  detail: string;
  requestId: string | null;
  retryable: boolean;
  recoveryCommand: string | null;
}

const DATABASE_CODES = new Set([
  "database_unavailable",
  "postgres_unavailable",
  "postgresql_unavailable",
]);
const NOT_READY_CODES = new Set([
  "service_not_ready",
  "service_unavailable",
  "not_ready",
]);

export function presentLoginError(
  cause: unknown,
  readiness: ReadyHealth | null
): ActionableError {
  if (cause instanceof ApiError && cause.status === 401) {
    return {
      title: "Incorrect username or password.",
      detail: "Check your credentials and try again.",
      requestId: cause.requestId,
      retryable: false,
      recoveryCommand: null,
    };
  }

  if (cause instanceof ApiError && (cause.status === 0 || cause.code === "api_unreachable")) {
    return {
      title: "Cannot reach the Observatory API.",
      detail: "The backend is not accepting requests.",
      requestId: cause.requestId,
      retryable: true,
      recoveryCommand: "agent-observatory up",
    };
  }

  const databaseDown = readiness?.components.postgresql?.status === "down";
  if (
    cause instanceof ApiError &&
    (DATABASE_CODES.has(cause.code) || (cause.status === 503 && databaseDown))
  ) {
    return {
      title: "PostgreSQL is unavailable.",
      detail: "Start PostgreSQL and run the system checks before signing in again.",
      requestId: cause.requestId,
      retryable: true,
      recoveryCommand: "agent-observatory doctor",
    };
  }

  if (
    cause instanceof ApiError &&
    (NOT_READY_CODES.has(cause.code) || cause.status === 503)
  ) {
    return {
      title: "Observatory is not ready.",
      detail:
        readiness?.recovery_actions[0] ??
        "A required service or background component is unavailable.",
      requestId: cause.requestId,
      retryable: true,
      recoveryCommand: "agent-observatory doctor",
    };
  }

  if (cause instanceof ApiError) {
    return {
      title: "Sign in failed.",
      detail: cause.message || "The server returned an unexpected error.",
      requestId: cause.requestId,
      retryable: cause.retryable,
      recoveryCommand: cause.status >= 500 ? "agent-observatory logs api" : null,
    };
  }

  return {
    title: "Sign in failed.",
    detail: cause instanceof Error ? cause.message : "An unexpected error occurred.",
    requestId: null,
    retryable: true,
    recoveryCommand: null,
  };
}
