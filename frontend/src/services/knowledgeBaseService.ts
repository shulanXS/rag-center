import { http } from "@/lib/http";
import type {
  ApiResponse,
  CreateKnowledgeBaseRequest,
  KnowledgeBaseData,
} from "@/types/api";

export async function createKnowledgeBase(payload: CreateKnowledgeBaseRequest) {
  const response = await http.post<ApiResponse<KnowledgeBaseData>>(
    "/api/v1/knowledge-bases/create",
    payload,
  );
  if (response.data.code !== 0) {
    throw new Error(response.data.msg);
  }
  return response.data.data;
}

