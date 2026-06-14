import { useState } from "react";
import { uploadDocument } from "@/services/documentService";
import type { UploadFileResult } from "@/types/api";
import { getRelativePath, isSupportedTextFile, readFileAsText } from "@/utils/file";

interface UploadDocumentsInput {
  tenantId: string;
  kbId: string;
  files: File[];
}

export function useUploadDocuments() {
  const [isUploading, setIsUploading] = useState(false);
  const [currentFile, setCurrentFile] = useState<string | null>(null);
  const [results, setResults] = useState<UploadFileResult[]>([]);

  async function uploadSequentially({ tenantId, kbId, files }: UploadDocumentsInput) {
    setIsUploading(true);
    setResults([]);

    const nextResults: UploadFileResult[] = [];

    for (const file of files) {
      const relativePath = getRelativePath(file);
      setCurrentFile(relativePath);

      if (!isSupportedTextFile(file)) {
        const result: UploadFileResult = {
          fileName: file.name,
          relativePath,
          status: "failed",
          message: "仅支持 .txt 和 .md 文件",
        };
        nextResults.push(result);
        setResults([...nextResults]);
        continue;
      }

      try {
        const content = await readFileAsText(file);
        const data = await uploadDocument({
          tenant_id: tenantId,
          kb_id: kbId,
          title: file.name,
          content,
        });
        const result: UploadFileResult = {
          fileName: file.name,
          relativePath,
          status: "success",
          documentId: data.document_id,
          chunkCount: data.chunk_count,
        };
        nextResults.push(result);
      } catch (error) {
        const result: UploadFileResult = {
          fileName: file.name,
          relativePath,
          status: "failed",
          message: error instanceof Error ? error.message : "上传失败",
        };
        nextResults.push(result);
      }

      setResults([...nextResults]);
    }

    setCurrentFile(null);
    setIsUploading(false);
    return nextResults;
  }

  return {
    currentFile,
    isUploading,
    results,
    uploadSequentially,
  };
}

