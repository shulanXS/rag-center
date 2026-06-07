curl -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "tenant_demo",
    "kb_id": "bbd1c945-1d2b-49be-a13d-893e4921b352",
    "user_id": "user_demo",
    "query": "退款需要几天内申请？"
  }'
