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

// ARCHITECTURE.md §19.3: "Every API response carries
// X-Dataset-Generation... the client stores the value it first saw and
// compares it on every response." M14's realtime owner is the single
// subscriber that acts on this (hard reinitialisation on mismatch) —
// this file only observes and reports every response's value, exactly
// once per request, success or failure alike, since a stale response
// to ANY call (not only a score/leaderboard fetch) must be detectable.
type DatasetGenerationListener = (generation: number) => void;
const generationListeners = new Set<DatasetGenerationListener>();

export function onDatasetGeneration(listener: DatasetGenerationListener): () => void {
  generationListeners.add(listener);
  return () => {
    generationListeners.delete(listener);
  };
}

function notifyDatasetGeneration(response: Response): void {
  const raw = response.headers.get("X-Dataset-Generation");
  if (raw === null) return;
  const generation = Number(raw);
  if (!Number.isFinite(generation)) return;
  for (const listener of generationListeners) listener(generation);
}

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

  notifyDatasetGeneration(response);

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

// A raw binary body (a photo): no multipart, no filename - the server decides what
// the bytes are from the bytes themselves, so the type sent here is only a label.
export function apiPutBinary<T>(path: string, body: Blob): Promise<ApiResult<T>> {
  return request<T>(path, {
    method: "PUT",
    headers: { "Content-Type": body.type || "application/octet-stream", Accept: "application/json" },
    body,
  });
}

export function apiRequest<T>(
  path: string,
  method: "PATCH" | "PUT" | "DELETE",
  payload?: unknown,
): Promise<ApiResult<T>> {
  return request<T>(path, {
    method,
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
}
