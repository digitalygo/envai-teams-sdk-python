# API inventory

All 52 current operation IDs, including experimental integration primitives.

| Operation ID | Method | Path |
|---|---|---|
| `sendChatMessage` | POST | `/chats/{chat_id}/messages` |
| `streamChatEvents` | GET | `/chats/{chat_id}/events` |
| `cancelChatTurn` | POST | `/chats/{chat_id}/cancel` |
| `steerChatTurn` | POST | `/chats/{chat_id}/steer` |
| `resolveChatTool` | POST | `/chats/{chat_id}/tool-output` |
| `resolveChatApproval` | POST | `/chats/{chat_id}/approval` |
| `forkChat` | POST | `/chats/{chat_id}/fork` |
| `restoreChat` | POST | `/chats/{chat_id}/restore` |
| `getCapabilities` | GET | `/capabilities` |
| `healthCheck` | GET | `/up` |
| `getJwks` | GET | `/.well-known/jwks.json` |
| `createWorkspace` | POST | `/workspaces` |
| `listWorkspaces` | GET | `/workspaces` |
| `getWorkspace` | GET | `/workspaces/{workspace_id}` |
| `updateWorkspace` | PUT | `/workspaces/{workspace_id}` |
| `createUserTool` | POST | `/tools` |
| `listUserTools` | GET | `/tools` |
| `getUserTool` | GET | `/tools/{name}` |
| `updateUserTool` | PUT | `/tools/{name}` |
| `deleteUserTool` | DELETE | `/tools/{name}` |
| `createChat` | POST | `/workspaces/{workspace_id}/chats` |
| `listChats` | GET | `/workspaces/{workspace_id}/chats` |
| `getChat` | GET | `/workspaces/{workspace_id}/chats/{chat_id}` |
| `listCompletions` | GET | `/workspaces/{workspace_id}/chats/{chat_id}/completions` |
| `getCompletion` | GET | `/workspaces/{workspace_id}/chats/{chat_id}/completions/{completion_id}` |
| `indexWorkspaceFiles` | GET | `/workspaces/{workspace_id}/files` |
| `createWorkspaceFile` | POST | `/workspaces/{workspace_id}/files` |
| `updateWorkspaceFile` | PUT | `/workspaces/{workspace_id}/files` |
| `deleteWorkspaceFile` | DELETE | `/workspaces/{workspace_id}/files` |
| `downloadWorkspaceFile` | GET | `/workspaces/{workspace_id}/files/download` |
| `convertWorkspaceFileToPdf` | POST | `/workspaces/{workspace_id}/files/convert-to-pdf` |
| `createAgent` | POST | `/agents` |
| `listAgents` | GET | `/agents` |
| `getAgent` | GET | `/agents/{agent_id}` |
| `updateAgent` | PUT | `/agents/{agent_id}` |
| `deleteAgent` | DELETE | `/agents/{agent_id}` |
| `createTeam` | POST | `/teams` |
| `listTeams` | GET | `/teams` |
| `getTeam` | GET | `/teams/{team_id}` |
| `updateTeam` | PUT | `/teams/{team_id}` |
| `deleteTeam` | DELETE | `/teams/{team_id}` |
| `listModels` | GET | `/models` |
| `createApiKey` | POST | `/api-keys` |
| `listApiKeys` | GET | `/api-keys` |
| `deleteApiKey` | DELETE | `/api-keys/{key_id}` |
| `testGenerateImage` | POST | `/tests/images/generate` |
| `createImageGeneration` | POST | `/images/generations` |
| `getImageGeneration` | GET | `/images/generations/{record}` |
| `uploadImage` | POST | `/images/uploads` |
| `listImageModels` | GET | `/images/models` |
| `downloadArtifact` | GET | `/artifacts/download` |
| `handleProviderWebhook` | POST | `/webhooks/providers/{provider}` |
