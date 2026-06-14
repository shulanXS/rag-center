export interface ApiResponse<T> {
  code: number;
  msg: string;
  data: T;
}

export interface CreateKnowledgeBaseRequest {
  name: string;
  description?: string;
  tenant_id: string;
}

export interface KnowledgeBaseData {
  kb_id: string;
  name: string;
  tenant_id: string;
  created_at: string;
}

export interface UploadDocumentRequest {
  tenant_id: string;
  kb_id: string;
  title: string;
  content: string;
}

export interface UploadDocumentData {
  document_id: string;
  kb_id: string;
  status: number;
  chunk_count: number;
}

export interface UploadFileResult {
  fileName: string;
  relativePath: string;
  status: "success" | "failed";
  documentId?: string;
  chunkCount?: number;
  message?: string;
}

