import { http } from "@/lib/http";
import type { ApiResponse, UploadDocumentData, UploadDocumentRequest } from "@/types/api";

export async function uploadDocument(payload: UploadDocumentRequest) {
  const response = await http.post<ApiResponse<UploadDocumentData>>(
    "/api/v1/documents/upload",
    payload,
  );
  if (response.data.code !== 0) {
    throw new Error(response.data.msg);
  }
  return response.data.data;
}

