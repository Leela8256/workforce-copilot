# Workforce Copilot — Multi-Agent Workplace Automation on RocketRide
A Slack-native backend that connects your team's Slack workspace, Gmail inbox, Jira board, and Google Calendar through a set of RocketRide pipelines. Every "thinking" step — ticket extraction, meeting scheduling, RAG Q&A — runs inside a RocketRide pipeline. Every external action is a real API call.

Three demo flows, no web UI:

- **Slack → Jira** — @-mention the bot with a task and it creates a real Jira ticket
- **Email → Calendar** — send a meeting request email and a Slack card appears within 30s with Chain-of-Debate slot proposals and one-click calendar booking
- **Slack RAG** — ask the bot a question and it searches past Jira tickets using vector similarity

---
## DEMO VIDEO: https://youtu.be/U5WjTXoDo74

## RocketRide pipelines

calendar_create_agent.pipe
<img width="893" height="421" alt="image" src="https://github.com/user-attachments/assets/d736da37-1273-4daa-9e8c-201c9a58f226" />

chain of debate implementation with 3 LLMS(prevents hallucination)
<img width="905" height="688" alt="image" src="https://github.com/user-attachments/assets/54dbcbac-9e2c-4c1e-8edd-b9b63cbca549" />

slack to Jira pipeline
<img width="888" height="506" alt="image" src="https://github.com/user-attachments/assets/589a7577-51e5-4edd-8c26-610311a994cf" />

RAG pipeline with slack and Jira tickets(query the ticket and message history)
<img width="882" height="172" alt="image" src="https://github.com/user-attachments/assets/5b26cca2-2845-4fd1-8984-59874c3e749c" />





## Project structure

```
project_RR/
├── pipelines/
│   ├── slack_to_jira_agent.pipe        # agent + tool_http_request → real Jira
│   ├── email_extract_intent.pipe       # chat → llm → structured meeting intent
│   ├── calendar_create_agent.pipe      # agent + tool_http_request → real Calendar
│   ├── cod_meeting_fanout.pipe         # 3 parallel agents (Proposer, Challenger, Judge)
│   ├── ingest_jira_history.pipe        # webhook → parse → anonymize → preprocessor → embedding → qdrant
│   └── team_qa_rag.pipe                # chat → embedding → qdrant → prompt → llm → answers
├── app.py                              # Slack Bolt (Socket Mode) — all three demo paths
├── rocketride_client.py                # async wrapper around the RocketRide SDK
├── gmail_poller.py                     # background thread: new email → meeting HITL flow
├── pending_meetings.py                 # in-memory session store for HITL button state
├── google_auth.py                      # per-user token refresh + participant_tokens_from_env()
├── slot_finder.py                      # multi-participant free/busy + displaceability scoring
├── seed_jira_corpus.py                 # seeds 20 synthetic past tickets into Qdrant
├── google_oauth_setup.py               # OAuth consent flow to mint a refresh token
├── requirements.txt
└── env.example
```

---

## RocketRide component coverage

| Component | Where it appears |
|---|---|
| `chat` | All action pipelines |
| `webhook` | `ingest_jira_history.pipe` |
| `llm_openai` (`openai-4o`) | All pipelines |
| `embedding_openai` (`text-embedding-3-small`) | RAG ingest + query |
| `agent_rocketride` | `slack_to_jira_agent`, `calendar_create_agent`, all 3 roles in `cod_meeting_fanout` |
| `memory_internal` | One per agent (5 instances) |
| `tool_http_request` | Jira POST + Calendar POST (URL whitelist + method gating) |
| `anonymize_text` | `ingest_jira_history.pipe` — PII scrub before vector storage |
| `parse` + `preprocessor_langchain` | RAG ingest |
| `qdrant` (local) | RAG ingest + query |
| `prompt` | `team_qa_rag.pipe` — merges retrieved tickets with user question |
| `response_answers` | All pipelines |
| **Pattern 1** (RAG Q&A) | `team_qa_rag.pipe` |
| **Pattern 3** (RAG ingest) | `ingest_jira_history.pipe` |
| **Pattern 8** (multi-agent fan-out) | `cod_meeting_fanout.pipe` — 3 agents → single `response_answers` |
| **Shared LLM** across invokers | `cod_meeting_fanout` — one `llm_shared` controlled by all three agents |
| **Env-var substitution** | `${ROCKETRIDE_OPENAI_KEY}`, `${ROCKETRIDE_JIRA_BASE_URL}`, etc. |

---

## How each flow works

### Slack → Jira
1. User types `@rocketride_assistant fix the safari login bug` in Slack
2. `app.py` receives the `app_mention` event and calls `slack_to_jira_agent.pipe`
3. The `agent_rocketride` decides if the message is actionable, extracts a summary and assignee hint, then calls `tool_http_request` to POST to the Jira REST API
4. Bot replies in thread: `✅ Created KAN-N — Fix safari login bug`

### Email → Calendar (autonomous + HITL)
```
1.  Email arrives at the monitored Gmail inbox
2.  gmail_poller (30s interval) detects it via users.history.list
3.  email_extract_intent.pipe  →  { is_meeting, title, attendees, time_preference, ... }
4.  slot_finder.py             →  walk Mon-Fri 9-5 across all participants' calendars
                                   classify: free > displaceable > partial
5.  cod_meeting_fanout.pipe    →  3 parallel agents read the same candidate_slots
                                   Proposer picks best slot, Challenger picks an alternative,
                                   Judge ranks top 3 — all via shared llm_openai
6.  app.py posts Slack card    →  CoD verdicts + 3 slot buttons + Cancel
7.  *** HITL — bot waits for a button click ***
8.  User clicks Slot N
9.  calendar_create_agent.pipe →  agent POSTs to Google Calendar API
10. Card updates in place      →  "✅ Calendar event created — <link>"
```

`cod_meeting_fanout.pipe` is Pattern 8 — three `agent_rocketride` nodes fanning out from one `chat` source, sharing a single `llm_openai` via the control plane, converging on one `response_answers`.

### Slack RAG
1. User types `@rocketride_assistant ask: have we hit a Safari issue before?`
2. `team_qa_rag.pipe` embeds the question, retrieves the closest tickets from Qdrant, merges them into a `prompt`, and the LLM answers with citations
3. Bot replies in thread with the answer

---

## Design decisions


**CoD as fan-out, not sequential.** All three agents (Proposer, Challenger, Judge) run in parallel on the same input instead of each seeing the previous one's output. This is faster and produces the visually striking Pattern 8 DAG. The Judge's top-3 ranking is used for the Slack card buttons.

**HITL via Slack interactivity.** RocketRide pipelines run to completion — there's no built-in pause. The pending meeting state is held in memory between the bot posting the card and the user clicking a button. This keeps the demo about RocketRide, not plumbing.

**PII scrubbed before vector storage.** `anonymize_text` runs between `parse` and `preprocessor_langchain` in the ingest pipeline. Names, emails, and phone numbers are masked before chunking and embedding.

**Single connection, all pipelines at boot.** `rocketride_client.py` pre-starts every pipeline with `ttl=0` (no idle timeout) and auto-restarts any pipeline that has gone stale before retrying the request.

---

## Setup

### Prerequisites
- Python 3.11+
- Docker Desktop
- RocketRide engine running (VS Code extension or `docker run ghcr.io/rocketride-org/rocketride-engine`)

### Quickstart

```bash
# 1. Create virtualenv and install deps
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Copy env.example to .env and fill in all values
#    Then precompute the Jira Basic auth header:
HEADER="Basic $(printf '%s' "$ROCKETRIDE_JIRA_EMAIL:$ROCKETRIDE_JIRA_API_TOKEN" | base64 | tr -d '\n')"
echo "ROCKETRIDE_JIRA_AUTH_HEADER=\"$HEADER\"" >> .env

# 3. Start Qdrant
docker run -d --name qdrant -p 6333:6333 -v "$(pwd)/.qdrant_data:/qdrant/storage" qdrant/qdrant:latest

# 4. Start the app
.venv/bin/python app.py

# 5. Seed the RAG corpus (one-time, run after app.py is up)
.venv/bin/python seed_jira_corpus.py
```

### Slack app (one-time)
1. [api.slack.com/apps](https://api.slack.com/apps) → Create New App → From scratch
2. **Bot Token Scopes**: `chat:write`, `chat:write.public`, `app_mentions:read`, `channels:history`
3. **Socket Mode** → Enable → generate App-Level Token with `connections:write`
4. **Event Subscriptions** → Enable → Add `app_mention`
5. Install to workspace, invite bot to your demo channel

### Google OAuth (one-time)
```bash
.venv/bin/python google_oauth_setup.py
# For a second calendar participant:
.venv/bin/python google_oauth_setup.py --participant
```
Writes a fresh `ROCKETRIDE_GOOGLE_REFRESH_TOKEN` to `.env`. Re-run if the token expires (Google Cloud test-mode tokens expire after 7 days).
