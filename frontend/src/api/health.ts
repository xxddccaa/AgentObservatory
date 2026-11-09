// Public liveness/readiness probes. They opt out of the global 401 handler and
// therefore never turn a health failure into an authentication redirect.
import { get } from "./client";

export type HealthState = "ok" | "degraded";
export type ComponentState = "up" | "down" | "stale" | "degraded";
export type HealthComponentName =
  | "postgresql"
  | "indexer";

export interface LiveHealth {
  status: "ok";
  version: string;
}

export interface HealthComponent {
  status: ComponentState;
  message?: string | null;
  last_seen_at?: string | null;
}

export interface ReadyHealth {
  status: HealthState;
  version: string;
  environment: string;
  components: Partial<Record<HealthComponentName, HealthComponent>>;
  recovery_actions: string[];
}

/** Compatibility alias used by existing views. */
export type Health = ReadyHealth;

export function getLiveHealth(): Promise<LiveHealth> {
  return get<LiveHealth>("/api/health/live", { suppressAuthHandler: true });
}

export function getReadyHealth(): Promise<ReadyHealth> {
  return get<ReadyHealth>("/api/health/ready", { suppressAuthHandler: true });
}

export function getHealth(): Promise<ReadyHealth> {
  return getReadyHealth();
}
