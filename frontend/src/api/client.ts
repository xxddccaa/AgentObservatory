// Shared frontend API client. Every typed helper goes through `request()`, so
// cookie handling, JSON parsing, request IDs, and error envelopes stay
// consistent across the application.

export interface ErrorEnvelope {
  error: {
    code: string;
    message: string;
    request_id?: string | null;
    retryable?: boolean;
  };
}

export class ApiError extends Error {
  readonly code: string;
  readonly requestId: string | null;
  readonly retryable: boolean;

  constructor(
    readonly status: number,
    message: string,
    readonly body?: unknown,
    options: { code?: string; requestId?: string | null; retryable?: boolean } = {}
  ) {
    super(message);
    this.name = "ApiError";
    this.code = options.code ?? (status === 0 ? "api_unreachable" : `http_${status}`);
    this.requestId = options.requestId ?? null;
    this.retryable = options.retryable ?? (status === 0 || status >= 500);
  }
}

export interface RequestOptions {
  /** Skip the global 401 handler (used by login + /me bootstrap). */
  suppressAuthHandler?: boolean;
}

type UnauthorizedHandler = () => void;
const unauthorizedHandlers = new Set<UnauthorizedHandler>();

/** Register a global handler invoked whenever a request returns 401. */
export function onUnauthorized(handler: UnauthorizedHandler): () => void {
  unauthorizedHandlers.add(handler);
  return () => unauthorizedHandlers.delete(handler);
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  opts: RequestOptions = {}
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(path, { credentials: "include", ...init, headers });
  } catch (cause) {
    throw new ApiError(0, "Cannot reach the Observatory API.", cause, {
      code: "api_unreachable",
      retryable: true,
    });
  }

  if (response.status === 401 && !opts.suppressAuthHandler) {
    unauthorizedHandlers.forEach((handler) => handler());
  }

  if (!response.ok) {
    const parsed = await extractError(response);
    throw new ApiError(response.status, parsed.message, parsed.body, {
      code: parsed.code,
      requestId: parsed.requestId,
      retryable: parsed.retryable,
    });
  }

  if (response.status === 204 || response.headers?.get?.("Content-Length") === "0") {
    return undefined as T;
  }
  return (await response.json()) as T;
}

interface ParsedError {
  code: string;
  message: string;
  requestId: string | null;
  retryable: boolean;
  body?: unknown;
}

async function extractError(response: Response): Promise<ParsedError> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = undefined;
  }

  const requestIdHeader = response.headers?.get?.("X-Request-ID") ?? null;
  if (isRecord(body) && isRecord(body.error)) {
    const error = body.error;
    return {
      code: typeof error.code === "string" ? error.code : `http_${response.status}`,
      message:
        typeof error.message === "string" ? error.message : `Request failed (${response.status})`,
      requestId:
        typeof error.request_id === "string" ? error.request_id : requestIdHeader,
      retryable: typeof error.retryable === "boolean" ? error.retryable : response.status >= 500,
      body,
    };
  }

  // Keep legacy FastAPI `detail` readable while the backend moves fully to the
  // public error envelope.
  const detail = isRecord(body) && typeof body.detail === "string" ? body.detail : null;
  return {
    code: `http_${response.status}`,
    message: detail ?? `Request failed (${response.status})`,
    requestId: requestIdHeader,
    retryable: response.status >= 500,
    body,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function get<T>(path: string, opts?: RequestOptions): Promise<T> {
  return request<T>(path, {}, opts);
}

export function post<T>(path: string, body?: unknown, opts?: RequestOptions): Promise<T> {
  return request<T>(
    path,
    { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) },
    opts
  );
}

export function patch<T>(path: string, body?: unknown, opts?: RequestOptions): Promise<T> {
  return request<T>(
    path,
    { method: "PATCH", body: body === undefined ? undefined : JSON.stringify(body) },
    opts
  );
}

export function del<T>(path: string, opts?: RequestOptions): Promise<T> {
  return request<T>(path, { method: "DELETE" }, opts);
}
