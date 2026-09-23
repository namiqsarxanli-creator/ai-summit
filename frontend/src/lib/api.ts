// Same-origin API client (ARCHITECTURE.md §3.2): every call is a
// relative /api/* path that the Next.js rewrite proxies to FastAPI —
// this code never knows a backend origin, and never needs CORS.

export type ApiError = {
  code: string;
  message: string;
  details: Record<string, unknown>;
};

export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; error: ApiError };

// A frontend-only code (§20.3): a failure that never reached the
// server is never dressed up as a fictional HTTP response.
export const NETWORK_ERROR = "NETWORK_ERROR";

function parseError(body: unknown): ApiError {
  if (typeof body === "object" && body !== null && "error" in body) {
    const error = (body as { error: unknown }).error;
    if (typeof error === "object" && error !== null && "code" in error) {
      const e = error as Partial<ApiError>;
      if (typeof e.code === "string") {
        return {
          code: e.code,
          message: typeof e.message === "string" ? e.message : "",
          details: typeof e.details === "object" && e.details !== null ? e.details : {},
        };
      }
    }
  }
  return { code: "INTERNAL_ERROR", message: "", details: {} };
}

async function request<T>(path: string, init: RequestInit): Promise<ApiResult<T>> {
  let response: Response;
  try {
    response = await fetch(path, { credentials: "same-origin", ...init });
  } catch {
    return { ok: false, status: 0, error: { code: NETWORK_ERROR, message: "", details: {} } };
  }

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }

  if (response.ok) {
    return { ok: true, data: body as T };
  }
  return { ok: false, status: response.status, error: parseError(body) };
}

export function apiGet<T>(path: string): Promise<ApiResult<T>> {
  return request<T>(path, { method: "GET", headers: { Accept: "application/json" } });
}

export function apiPost<T>(path: string, payload: unknown): Promise<ApiResult<T>> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(payload),
  });
}
