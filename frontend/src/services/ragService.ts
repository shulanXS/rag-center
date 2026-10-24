import { http } from "@/lib/http";
import type {
  ApiResponse,
  FeedbackData,
  FeedbackRequest,
  RetrieveData,
  RetrieveRequest,
  ResearchData,
  ResearchRequest,
} from "@/types/api";

export type ResearchStatus = "pending" | "running" | "completed" | "failed";

export interface ResearchSubmitResponse {
  research_id: string;
  status: ResearchStatus;
}

export interface ResearchPendingResponse {
  research_id: string;
  status: ResearchStatus;
  progress?: unknown;
}

export async function retrieve(payload: RetrieveRequest) {
  const response = await http.post<ApiResponse<RetrieveData>>(
    "/api/v1/rag/retrieve",
    payload,
  );
  if (response.data.code !== 0) {
    throw new Error(response.data.msg);
  }
  return response.data.data;
}

export async function submitResearch(
  payload: ResearchRequest,
): Promise<ResearchSubmitResponse> {
  const response = await http.post<ApiResponse<ResearchSubmitResponse>>(
    "/api/v1/rag/research",
    payload,
  );
  if (response.data.code !== 0) {
    throw new Error(response.data.msg);
  }
  return response.data.data;
}

export async function getResearchResult(
  researchId: string,
  signal?: AbortSignal,
): Promise<ResearchData> {
  const response = await http.get<ApiResponse<ResearchData>>(
    `/api/v1/rag/research/${encodeURIComponent(researchId)}`,
    { signal },
  );
  if (response.data.code !== 0) {
    throw new Error(response.data.msg);
  }
  return response.data.data;
}

/**
 * Submit a research request then poll for the result.
 *
 * Returns the full ResearchData once the backend Celery worker has
 * finished. The polling interval is 3 seconds and the call is fully
 * cancellable via the AbortSignal.
 */
export async function research(
  payload: ResearchRequest,
  options: {
    pollIntervalMs?: number;
    signal?: AbortSignal;
  } = {},
): Promise<ResearchData> {
  const interval = options.pollIntervalMs ?? 3000;
  const submitted = await submitResearch(payload);
  while (true) {
    if (options.signal?.aborted) {
      throw new DOMException("Research polling aborted", "AbortError");
    }
    // Sleep with cancellation support
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(resolve, interval);
      if (options.signal) {
        const onAbort = () => {
          clearTimeout(timer);
          reject(new DOMException("Research polling aborted", "AbortError"));
        };
        options.signal.addEventListener("abort", onAbort, { once: true });
      }
    });
    if (options.signal?.aborted) {
      throw new DOMException("Research polling aborted", "AbortError");
    }
    try {
      return await getResearchResult(submitted.research_id, options.signal);
    } catch (error) {
      if (
        options.signal?.aborted ||
        (error instanceof DOMException && error.name === "AbortError")
      ) {
        throw error;
      }
      // Network / 5xx transient failures: continue polling. Permanent
      // failures (e.g. RESEARCH_FAILED) propagate out of getResearchResult.
      // eslint-disable-next-line no-console
      console.warn("Research poll failed, retrying:", error);
    }
  }
}

export async function submitFeedback(payload: FeedbackRequest) {
  const response = await http.post<ApiResponse<FeedbackData>>(
    "/api/v1/rag/feedback",
    payload,
  );
  if (response.data.code !== 0) {
    throw new Error(response.data.msg);
  }
  return response.data.data;
}
