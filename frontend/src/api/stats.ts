import { get } from "./client";

export type StatsRange = "day" | "week" | "month";

export interface TokenUsageTotals {
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  reasoning_output_tokens: number;
  total_tokens: number;
  session_count: number;
  turn_count: number;
}

export interface TokenUsageResponse {
  range: StatsRange;
  since: string;
  until: string;
  totals: TokenUsageTotals;
}

export function getTokenUsage(range: StatsRange = "day"): Promise<TokenUsageResponse> {
  return get<TokenUsageResponse>(`/api/stats/token-usage?range=${range}`);
}
