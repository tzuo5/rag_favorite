# Gordon-Core-Tokyo: Phase 5.2 Baseline

- Generated UTC: `2026-07-19T07:19:10Z`
- Phase: `5.2 Governed MCP Mutation Layer`
- Baseline status: `CAPTURED`
- Mutation implementation status: `NOT_STARTED`

## 1. Confirmed Architecture

```text
OpenClaw gateway
  -> Python MCP server over stdio
     /home/ubuntu/services/rag-mcp/rag_mcp_server.py
  -> subprocess with shell=false
     /home/ubuntu/services/rag-app/rag.py
  -> psycopg
     PostgreSQL container: rag-postgres

MCP language=Python 3.12
MCP SDK=mcp/FastMCP 1.28.1
Registered tools=rag_status,rag_search
Mutation tools=none
Transport=stdio subprocess
Gateway service=systemd user service
```

## 2. Relevant Source Tree

```text
mcp_smoke_test.py|type=f|mode=600|owner=ubuntu:ubuntu|size=2325
rag_mcp_server.py|type=f|mode=600|owner=ubuntu:ubuntu|size=6934
requirements.txt|type=f|mode=600|owner=ubuntu:ubuntu|size=12
routing_eval.py|type=f|mode=600|owner=ubuntu:ubuntu|size=8631
routing_eval_v2.py|type=f|mode=600|owner=ubuntu:ubuntu|size=7869
routing_eval_v3.py|type=f|mode=600|owner=ubuntu:ubuntu|size=5216
rag-app/.gitignore|type=f|mode=664|owner=ubuntu:ubuntu|size=31
rag-app/rag.py|type=f|mode=700|owner=ubuntu:ubuntu|size=20505
rag-app/requirements.txt|type=f|mode=664|owner=ubuntu:ubuntu|size=100
rag-app/|type=d|mode=775|owner=ubuntu:ubuntu|size=4096
rag-app/.venv/bin|type=d|mode=775|owner=ubuntu:ubuntu|size=4096
rag-app/.venv/include|type=d|mode=775|owner=ubuntu:ubuntu|size=4096
rag-app/.venv/lib64|type=l|mode=777|owner=ubuntu:ubuntu|size=3
rag-app/.venv/lib|type=d|mode=775|owner=ubuntu:ubuntu|size=4096
rag-app/.venv/pyvenv.cfg|type=f|mode=664|owner=ubuntu:ubuntu|size=174
rag-app/.venv|type=d|mode=775|owner=ubuntu:ubuntu|size=4096
```

## 3. Key File Hashes

```text
7187acc8b650caf62730a8ebd628c5a9e1ef1a2e59c849f3e8ec193bb034b2e2  /home/ubuntu/services/rag-mcp/rag_mcp_server.py
4790f4e033e502e2bfff6f523e1a366438df8e1a6d51086bf14aaf8e09a4f737  /home/ubuntu/services/rag-mcp/mcp_smoke_test.py
59f9f112b90ea7b1a4ec255972de0a673f3aecbb93a8f924cac1a8fe1f5e184f  /home/ubuntu/services/rag-mcp/requirements.txt
80cc38a01bd908c182e8b2a6bf27ddb93499c266ee68294eb08c875ac8a57a2a  /home/ubuntu/services/rag-app/rag.py
a6ec229a5b047089e151828a6fce827d691d7d8b25337fe5c4c2799529c1db15  /home/ubuntu/services/rag-app/requirements.txt
2c0e4f09c691db2c948e6cce2d6308ee3d07346f679b9edfada4673a41bc17f7  /home/ubuntu/.openclaw/openclaw.json
2e0f402c6bb3436e876d1e83f82512cde70aa954d36daec6f14003eada8eb574  /home/ubuntu/.config/systemd/user/openclaw-gateway.service
2c9363290de14643aa624e3e168d182894bf8c792da0af4da26ec1eb592fd06c  /home/ubuntu/services/rag-postgres/compose.yaml
secret-file-metadata path=/home/ubuntu/.openclaw/.env mode=600 owner=ubuntu:ubuntu size=66
secret-file-sha256 51c76bf7cd89803130bb48ca212a3af277de10a7af75254522621d4311e76f71  /home/ubuntu/.openclaw/.env
secret-file-metadata path=/home/ubuntu/services/rag-postgres/.env mode=600 owner=ubuntu:docker size=109
secret-file-sha256 12c7029686d8bf30187b01244895544adca45b554efd0cb9b7e3aaaf760428e6  /home/ubuntu/services/rag-postgres/.env
```

## 4. OpenClaw MCP Configuration

```text
agent_id="main"
mcp_command="/home/ubuntu/services/rag-mcp/.venv/bin/python"
mcp_args=["/home/ubuntu/services/rag-mcp/rag_mcp_server.py"]
mcp_cwd="/home/ubuntu/services/rag-mcp"
connect_timeout=30
tool_timeout=180
server_tool_filter_include=["rag_search", "rag_status"]
agent_sandbox_also_allow=["gordon-rag__rag_search", "gordon-rag__rag_status"]
```

## 5. Live MCP Read Behavior

```json
{
  "search_content_items": 1,
  "search_is_error": false,
  "status_chunks": 4,
  "status_documents": 4,
  "status_healthy": true,
  "status_is_error": false,
  "tool_names": [
    "rag_search",
    "rag_status"
  ]
}
```

## 6. Runtime State

```text
openclaw_version=OpenClaw 2026.7.1 (2d2ddc4)
MainPID=1699268
LoadState=loaded
ActiveState=active
SubState=running
FragmentPath=/home/ubuntu/.config/systemd/user/openclaw-gateway.service
container=rag-postgres image=pgvector/pgvector:0.8.2-pg16-bookworm status=Up 2 days (healthy) networks=rag-postgres_default
{"ConfigFiles": "/home/ubuntu/services/ollama/compose.yaml", "Name": "ollama", "Status": "running(1)"}
{"ConfigFiles": "/home/ubuntu/services/rag-postgres/compose.yaml", "Name": "rag-postgres", "Status": "running(1)"}
```

## 7. Database Role and Grants

```text
BEGIN
connection_identity|ragdb|rag_admin|rag_admin
role_attributes|rag_admin|t|t|t|t|t|t|t
database_owner|ragdb|rag_admin
table_privileges|rag_chunks|t|t|t|t|t|rag_admin
table_privileges|rag_documents|t|t|t|t|t|rag_admin
table_privileges|rag_memories|t|t|t|t|t|rag_admin
table_privileges|rag_memory_events|t|t|t|t|t|rag_admin
table_privileges|rag_memory_revisions|t|t|t|t|t|rag_admin
rag_routine|rag_reject_immutable_change|f|rag_admin|t
rag_routine|rag_validate_revision_chain|f|rag_admin|t
COMMIT
```

## 8. Governance Schema State

```text
BEGIN
row_counts|4|4|0|0|0
governed_trigger_count|5
critical_cross_table_constraint_count|2
idle_in_transaction_connections|0
COMMIT
```

## 9. Baseline Findings

```text
existing_read_tools=rag_status,rag_search
existing_agent_accessible_mutation_tools=none
existing_mcp_write_repository=none
existing_mcp_database_role=rag_admin
existing_mcp_database_role_is_superuser=true
existing_mcp_database_role_is_table_owner=true
dedicated_reader_role=false
dedicated_writer_role=false
revision_event_immutability_triggers=present
governed_memory_tables=present
phase_5_2_mutation_code_started=false
```

## 10. Baseline Verification

```text
PHASE_5_2_STEP_1_READ_ONLY_INVENTORY=COMPLETE
PHASE_5_2_STEP_2_BASELINE_CAPTURE=PASS
```
