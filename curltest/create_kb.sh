curl -X POST http://127.0.0.1:8000/api/v1/knowledge-bases/create \
  -H "Content-Type: application/json" \
  -d '{
    "name": "退款政策知识库",
    "description": "用于客服退款问题问答",
    "tenant_id": "tenant_demo"
  }'

