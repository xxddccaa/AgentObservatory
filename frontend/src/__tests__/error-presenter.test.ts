import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import { presentLoginError } from "../api/error-presenter";
import type { ReadyHealth } from "../api/health";

const degraded: ReadyHealth = {
  status: "degraded",
  version: "0.1.0",
  environment: "test",
  components: {
    postgresql: { status: "down", message: "connection refused" },
    indexer: { status: "up" },
  },
  recovery_actions: ["Start PostgreSQL."],
};

describe("presentLoginError", () => {
  it("keeps invalid credentials distinct from infrastructure failures", () => {
    expect(presentLoginError(new ApiError(401, "invalid"), null)).toMatchObject({
      title: "Incorrect username or password.",
      retryable: false,
      recoveryCommand: null,
    });
  });

  it("suggests the lifecycle command when the API cannot be reached", () => {
    expect(presentLoginError(new ApiError(0, "offline"), null)).toMatchObject({
      title: "Cannot reach the Observatory API.",
      retryable: true,
      recoveryCommand: "agent-observatory up",
    });
  });

  it("uses readiness state and preserves request IDs for database failures", () => {
    const error = new ApiError(503, "not ready", undefined, {
      code: "service_not_ready",
      requestId: "req-db",
      retryable: true,
    });
    expect(presentLoginError(error, degraded)).toMatchObject({
      title: "PostgreSQL is unavailable.",
      requestId: "req-db",
      recoveryCommand: "agent-observatory doctor",
    });
  });
});
