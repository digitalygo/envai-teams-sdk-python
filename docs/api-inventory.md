# API inventory

The reviewed 92 SDK operation IDs (89 callable operations plus 3 unsupported API-key management stubs), selected from the 146-operation server contract pinned at backend commit `ba021aa61e30e379880f809d45f90e5d3564d43e` (`digitalygo/1-1-5-envai-aaas`). This source is unreleased; the `1.0.0a6` baseline contains 78 operations. The 14 operations added by this source are marked **new**. Canonical ATP operations use contract3; the two explicit legacy reads require read-only contract2. Public Wiki methods are negotiated; Wiki JWKS is public. No internal Decisions QUERY, artifact-write, or CAS endpoint is invented.

| Operation ID | Method | Path |
| --- | --- | --- |
| deliverWorkflowResult | POST | `/atp/v3/chats/{chat_id}/deliveries` |
| delegateToTeam | POST | `/atp/v3/chats/{chat_id}/delegations` |
| getDetachedTask | GET | `/atp/v3/chats/{chat_id}/tasks/{task_id}` |
| cancelDetachedTask | POST | `/atp/v3/chats/{chat_id}/tasks/{task_id}/cancel` |
| classifyPhase | POST | `/atp/v3/chats/{chat_id}/classify-phase` |
| classifyChoice | POST | `/atp/v3/chats/{chat_id}/classify-choice` |
| startIndependentJob | POST | `/atp/v3/chats/{chat_id}/jobs` |
| sendChatMessage | POST | `/atp/v3/chats/{chat_id}/messages` |
| streamChatEvents | GET | `/atp/v3/chats/{chat_id}/events` |
| cancelChatTurn | POST | `/atp/v3/chats/{chat_id}/cancel` |
| steerChatTurn | POST | `/atp/v3/chats/{chat_id}/steer` |
| resolveChatTool | POST | `/atp/v3/chats/{chat_id}/tool-output` |
| resolveChatApproval | POST | `/atp/v3/chats/{chat_id}/approval` |
| forkChat | POST | `/atp/v3/chats/{chat_id}/fork` |
| restoreChat | POST | `/atp/v3/chats/{chat_id}/restore` |
| getCapabilities | GET | `/atp/v3/capabilities` |
| healthCheck | GET | `/up` |
| getJwks | GET | `/.well-known/jwks.json` |
| createWorkspace | POST | `/workspaces` |
| listWorkspaces | GET | `/workspaces` |
| getWorkspace | GET | `/workspaces/{workspace_id}` |
| updateWorkspace | PUT | `/workspaces/{workspace_id}` |
| deleteWorkspace | DELETE | `/workspaces/{workspace_id}` (**new**, 200 cleanup result) |
| createProjectTool | POST | `/tools` |
| listProjectTools | GET | `/tools` |
| getProjectTool | GET | `/tools/{name}` |
| updateProjectTool | PUT | `/tools/{name}` |
| deleteProjectTool | DELETE | `/tools/{name}` |
| createChat | POST | `/workspaces/{workspace_id}/chats` |
| listChats | GET | `/workspaces/{workspace_id}/chats` |
| getChat | GET | `/workspaces/{workspace_id}/chats/{chat_id}` |
| updateChat | PUT | `/workspaces/{workspace_id}/chats/{chat_id}` (**new**, title only) |
| deleteChat | DELETE | `/workspaces/{workspace_id}/chats/{chat_id}` (**new**, 200 cleanup result) |
| listChatCrons | GET | `/workspaces/{workspace_id}/crons` (**new**) |
| getChatCron | GET | `/workspaces/{workspace_id}/crons/{cron_id}` (**new**) |
| updateChatCron | PATCH | `/workspaces/{workspace_id}/crons/{cron_id}` (**new**) |
| deleteChatCron | DELETE | `/workspaces/{workspace_id}/crons/{cron_id}` (**new**, 204) |
| listCompletions | GET | `/workspaces/{workspace_id}/chats/{chat_id}/completions` |
| getCompletion | GET | `/workspaces/{workspace_id}/chats/{chat_id}/completions/{completion_id}` |
| indexWorkspaceFiles | GET | `/workspaces/{workspace_id}/files` |
| createWorkspaceFile | POST | `/workspaces/{workspace_id}/files` |
| updateWorkspaceFile | PUT | `/workspaces/{workspace_id}/files` |
| deleteWorkspaceFile | DELETE | `/workspaces/{workspace_id}/files` |
| downloadWorkspaceFile | GET | `/workspaces/{workspace_id}/files/download` |
| convertWorkspaceFileToPdf | POST | `/workspaces/{workspace_id}/files/convert-to-pdf` |
| createAgent | POST | `/agents` |
| listAgents | GET | `/agents` |
| getAgent | GET | `/agents/{agent_id}` |
| updateAgent | PUT | `/agents/{agent_id}` |
| deleteAgent | DELETE | `/agents/{agent_id}` |
| createTeam | POST | `/teams` |
| listTeams | GET | `/teams` |
| getTeam | GET | `/teams/{team_id}` |
| updateTeam | PUT | `/teams/{team_id}` |
| deleteTeam | DELETE | `/teams/{team_id}` |
| listModels | GET | `/models` |
| createApiKey | POST | `/api-keys` (unsupported: human session + CSRF) |
| listApiKeys | GET | `/api-keys` (unsupported: human session + CSRF) |
| deleteApiKey | DELETE | `/api-keys/{key_id}` (unsupported: human session + CSRF) |
| testGenerateImage | POST | `/tests/images/generate` |
| createImageGeneration | POST | `/images/generations` |
| getImageGeneration | GET | `/images/generations/{record}` |
| uploadImage | POST | `/images/uploads` |
| listImageModels | GET | `/images/models` |
| downloadArtifact | GET | `/artifacts/download` |
| handleProviderWebhook | POST | `/webhooks/providers/{provider}` |
| uploadChatAttachment | POST | `/workspaces/{workspace_id}/chats/{chat_id}/attachments` |
| listChatAttachments | GET | `/workspaces/{workspace_id}/chats/{chat_id}/attachments` |
| getChatAttachment | GET | `/workspaces/{workspace_id}/chats/{chat_id}/attachments/{attachment_id}` |
| deleteChatAttachment | DELETE | `/workspaces/{workspace_id}/chats/{chat_id}/attachments/{attachment_id}` |
| retryChatAttachment | POST | `/workspaces/{workspace_id}/chats/{chat_id}/attachments/{attachment_id}/retry` |
| downloadChatAttachment | GET | `/workspaces/{workspace_id}/chats/{chat_id}/attachments/{attachment_id}/download` |
| uploadWikiSource | POST | `/workspaces/{workspace_id}/files/sources` |
| getWikiSource | GET | `/workspaces/{workspace_id}/files/sources/{file_id}` |
| downloadWikiSource | GET | `/workspaces/{workspace_id}/files/sources/{file_id}/download` |
| withdrawWikiSource | POST | `/workspaces/{workspace_id}/wiki/outbox/files` |
| createWikiInboxRun | POST | `/workspaces/{workspace_id}/wiki/inbox/runs` |
| createWikiOutboxRun | POST | `/workspaces/{workspace_id}/wiki/outbox/runs` |
| createWikiSyncRun | POST | `/workspaces/{workspace_id}/wiki/syncs` |
| getWikiOperation | GET | `/workspaces/{workspace_id}/wiki/operations/{operation_id}` |
| listWikiCategories | GET | `/workspaces/{workspace_id}/wiki/categories` |
| getWikiCategoryIndex | GET | `/workspaces/{workspace_id}/wiki/categories/{category_id}/index` |
| getWikiWebhookJwks | GET | `/.well-known/wiki-webhook-jwks.json` |
| listProjectSkills | GET | `/projects/{project_id}/skills` (**new**) |
| createProjectSkill | POST | `/projects/{project_id}/skills` (**new**) |
| getProjectSkill | GET | `/projects/{project_id}/skills/{skill_id}` (**new**) |
| updateProjectSkill | PUT | `/projects/{project_id}/skills/{skill_id}` (**new**) |
| deleteProjectSkill | DELETE | `/projects/{project_id}/skills/{skill_id}` (**new**, 204) |
| getProjectUsage | GET | `/projects/{project_id}/usage` (**new**) |
| getProjectLogs | GET | `/projects/{project_id}/logs` (**new**, `view` selects events, analytics or filters) |
| streamChatEventsLegacy | GET | `/chats/{chat_id}/events` |
| getCapabilitiesLegacy | GET | `/capabilities` |

## Method names for the new operations

| Operation ID | Primitive (both clients) | Service group method |
| --- | --- | --- |
| deleteWorkspace | `delete_workspace` | `workspaces.delete` |
| updateChat | `update_chat` | `chats.update` |
| deleteChat | `delete_chat` | `chats.delete` |
| listChatCrons | `list_chat_crons` | `chat_crons.list` |
| getChatCron | `get_chat_cron` | `chat_crons.get` |
| updateChatCron | `update_chat_cron` | `chat_crons.update` |
| deleteChatCron | `delete_chat_cron` | `chat_crons.delete` |
| listProjectSkills | `list_project_skills` | `project_skills.list` |
| createProjectSkill | `create_project_skill` | `project_skills.create` |
| getProjectSkill | `get_project_skill` | `project_skills.get` |
| updateProjectSkill | `update_project_skill` | `project_skills.update` |
| deleteProjectSkill | `delete_project_skill` | `project_skills.delete` |
| getProjectUsage | `get_project_usage` | `project_reports.usage` (handwritten helper) |
| getProjectLogs | `get_project_logs` | `project_reports.logs`, `.analytics`, `.filters` (handwritten helpers) |

The generated `ProjectReportsService` in `_generated.py` is replaced on the client by the handwritten helper described in the [API reference](api-reference.md#project-reports). Use `get_project_usage` and `get_project_logs` for the raw operations. `get_project_logs` returns the wrapped union `ProjectLogCollection | ProjectActivityAnalyticsResource | ProjectEventTypeFiltersResource` without unwrapping `data`.

## Event catalog

The pinned event snapshot defines 39 typed ATP event types. `chat.title.updated`, `chat.cron.wakeup`, `billing.limit_reached` and `billing.credit_exhausted` were already typed in `envai.event_models` before this source; they are not new classes. Only `chat.title.updated` exists in the frozen ATP2 catalog.
