import { Navigate, Route, Routes } from "react-router-dom";
import { KnowledgeUploadPage } from "@/pages/KnowledgeUploadPage";

export function App() {
  return (
    <Routes>
      <Route path="/" element={<KnowledgeUploadPage />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

