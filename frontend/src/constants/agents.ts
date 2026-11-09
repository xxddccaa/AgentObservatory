export const AGENT_OPTIONS = [
  { value: "", label: "All agents" },
  { value: "codex", label: "Codex" },
  { value: "stepcode", label: "StepCode" },
  { value: "stepcli", label: "Step CLI" },
  { value: "claude", label: "Claude" },
  { value: "dsh", label: "DSH" },
  { value: "opencode", label: "OpenCode" },
  { value: "pi", label: "Pi" },
  { value: "xdcode", label: "XDCode" },
  { value: "stepgo", label: "StepGo" },
  { value: "generic", label: "Generic" },
] as const;

export type AgentFilter = (typeof AGENT_OPTIONS)[number]["value"];

export function agentLabel(agent: string | null | undefined): string {
  const normalized = agent?.trim().toLowerCase();
  if (!normalized) return "Unknown";
  return AGENT_OPTIONS.find((option) => option.value === normalized)?.label ?? agent!.trim();
}
