# Base tecnica per gli SDK Envai API

## 1. Scopo

Questo documento definisce il comportamento che gli SDK ufficiali Envai devono esporre in modo coerente nei diversi linguaggi. Descrive l'API presente sul branch `alpha`, il modello Agent Team Protocol (ATP), il trasporto HTTP e SSE, la gestione degli errori, i client tool, le responsabilità degli SDK e una suite minima di conformità.

L'analisi di base è stata eseguita il 27 agosto 2026 su `origin/alpha` al commit `cc2e723`, contratto API `2.0.0` ed envelope ATP `v: 2`. Le correzioni normative descritte nella sezione 23 fanno parte dello stesso intervento e prevalgono sullo snapshot iniziale.

Non sostituisce i contratti machine-readable. Lo scopo è trasformare quei contratti in una guida implementativa unica, evitando che ogni SDK prenda decisioni diverse su autenticazione, retry, streaming, firme, errori o concorrenza.

Gli SDK iniziali previsti sono:

- TypeScript: `@digitalygo/envai-client`
- Python: `digitalygo/envai-client`
- Go: `github.com/digitalygo/envai-client`

## 2. Fonti normative e precedenza

Gli SDK devono essere sviluppati contro queste fonti, in ordine di precedenza:

1. `packages/contracts/openapi.yaml` per endpoint HTTP, parametri, request, response, status code e autenticazione.
2. `packages/contracts/events.json` per envelope ATP, tipi evento e payload dello stream.
3. `docs/agent-team-protocol.md` per semantica, lifecycle, ordinamento, sospensione dei branch, restore e fork.
4. Implementazione e test in `apps/execution-plane/internal/gateway`, `apps/execution-plane/internal/chat` e `apps/control-plane` per verificare lo stato realmente disponibile su `alpha`.

Quando una descrizione narrativa diverge da uno schema machine-readable, l'SDK deve seguire lo schema e la divergenza deve essere corretta nel repository. Un SDK non deve inventare campi o comportamenti per compensare silenziosamente una contraddizione.

I file `docs/openapi.yaml` e le superfici legacy sotto `/v1` sono storici. Il contratto combinato corrente è `packages/contracts/openapi.yaml` e il prefisso pubblico è `/api/v1`.

## 3. Architettura dell'API

Envai è divisa in due piani dietro un unico edge.

Il control plane Laravel gestisce risorse persistenti e configurazione: workspace, chat, definizioni agente, team, client tool, API key, cataloghi modelli, metadata delle completion, workspace files e immagini.

L'execution plane Go gestisce l'esecuzione ATP: accetta messaggi, apre turni, orchestra gli agenti, espone lo stream SSE e riceve cancel, steer, output dei client tool, restore e fork. Lo stream non passa mai da PHP.

In produzione lo SDK deve usare un solo `base_url` dell'edge. In sviluppo è possibile usare direttamente i due servizi, ma ciò richiede due URL distinti. Il server OpenAPI predefinito `http://localhost:8111/api/v1` rappresenta l'edge locale. `http://localhost:8081/api/v1` raggiunge direttamente il gateway Go e quindi non deve essere usato per i metodi del control plane.

Il client deve rendere `base_url` configurabile, normalizzarlo rimuovendo lo slash finale e aggiungere i path senza duplicare `/api/v1`.

## 4. Modello concettuale

### 4.1 Workspace

Il workspace è il confine di isolamento. Contiene lo stato durevole usato dagli agenti, compresi wiki, skill e `MEMORY.md`, esposti come file attraverso la workspace files API. Un workspace non seleziona un team globale.

### 4.2 Team e agenti

Un team contiene un orchestratore e zero o più membri. Le definizioni agente stabiliscono prompt, modello opzionale, modalità, strumenti built-in, client tool esterni, permessi e possibilità di scrivere conoscenza o skill.

Il team viene scelto quando si crea una chat. `team_id` è obbligatorio. L'execution plane non usa un team di fallback quando la chat non ha un team valido.

### 4.3 Chat

La chat è l'unica entità di esecuzione persistente. Contiene una cronologia e ospita molti turni. Non esiste un'entità parallela denominata run o sessione.

La chat viene creata sul control plane:

```http
POST /api/v1/workspaces/{workspace_id}/chats
Content-Type: application/json
Authorization: Bearer <api-key>

{"team_id":"team_<ulid>"}
```

### 4.4 Turno e completion

Un messaggio apre un turno:

```http
POST /api/v1/chats/{chat_id}/messages
Content-Type: application/json
Authorization: Bearer <api-key>

{"message":"Analizza questi dati"}
```

La risposta `202 Accepted` contiene il `turn_id`. Il turn id coincide con l'identificatore della completion dell'orchestratore e usa il prefisso `cmpl_`.

La completion è il record metadata-only del turno. Non contiene messaggi, delta, reasoning o output dei tool. È consultabile dagli endpoint del control plane e contiene stato, modelli, tempi, usage, costo e un eventuale hash del contenuto.

È consentito un solo turno attivo per chat. Chat diverse dello stesso workspace possono eseguire turni in parallelo.

### 4.5 Istanza agente

Gli eventi rappresentano istanze in esecuzione, non definizioni persistenti. Due subagent dello stesso tipo possono avere lo stesso `name` ma `instance_id` differenti. `parent_id` ricostruisce l'albero di delegazione.

## 5. Identificatori

Gli identificatori di dominio sono stringhe ULID con prefisso. Gli SDK devono trattarli come tipi nominali o alias di stringa, senza convertirli in UUID.

| Entità | Forma |
| --- | --- |
| Workspace | `wsp_<ulid>` |
| Chat | `cht_<ulid>` |
| Completion e turno | `cmpl_<ulid>` |
| Definizione agente | `agt_<ulid>` |
| Team | `team_<ulid>` |
| API key | `key_<ulid>` |
| Generazione immagine | `igr_<ulid>` |
| Upload immagine | `upl_<ulid>` |
| Agente in esecuzione | `ag_<ulid>` |
| Evento | `evt_<ulid>` |
| Client tool call | `tc_<ulid>` |
| Workspace tool | `twl_<ulid>` |
| Utente | `usr_<ulid>` |
| Chiave di firma | `sig_<ulid>` |

La validazione locale degli identificatori può migliorare l'esperienza, ma non deve sostituire la validazione server. Gli SDK devono conservare la stringa originale e non cambiarne mai maiuscole o minuscole.

## 6. Autenticazione

L'autenticazione programmatica usa una API key JWT a lunga durata, restituita una sola volta da `POST /api/v1/api-keys` nella forma `envai_sk_<ulid><jwt>`.

Ogni richiesta autenticata deve inviare:

```http
Authorization: Bearer envai_sk_...
```

Il dashboard può usare una sessione Laravel, ma gli SDK ufficiali devono privilegiare il bearer token. Il costruttore del client deve accettare la chiave esplicitamente o tramite una configurazione esterna al repository. Non deve leggerla da log, serializzarla nelle eccezioni o includerla nella rappresentazione testuale del client.

La revoca avviene con `DELETE /api/v1/api-keys/{key_id}`. Nella creazione, omettere `expires_at` applica il default corrente `30d`; soltanto il valore esplicito `"never"` produce `expires_at: null` e una chiave valida fino alla revoca. Gli SDK devono distinguere omissione e durata illimitata.

`GET /.well-known/jwks.json` è pubblico e fornisce le chiavi Ed25519 necessarie anche per verificare i client tool. Il path JWKS non è sotto `/api/v1`. Lo SDK deve risolverlo rispetto all'origine del `base_url`, non concatenarlo al prefisso API, e deve permettere un `jwks_url` esplicito per deployment con origini separate.

## 7. Client HTTP comune

Ogni SDK deve avere un core HTTP condiviso tra tutti i servizi. Il core deve:

- aggiungere autenticazione, `Accept` e `User-Agent`
- serializzare JSON e multipart senza cambiare i nomi dei campi
- distinguere risposte JSON, binarie, vuote e SSE
- applicare timeout separati per connessione, richiesta normale e stream
- trasformare l'envelope `Error` in eccezioni tipizzate
- preservare status code, `error.type`, `error.code`, `error.message` e `error.details`
- permettere l'iniezione di un trasporto HTTP per test e ambienti personalizzati
- supportare cancellazione nativa del linguaggio

Il `User-Agent` consigliato è:

```text
envai-<language>/<sdk-version> (<runtime>/<runtime-version>)
```

Le risposte del control plane usano normalmente `{ "data": ... }`. Le collection non paginate usano `{ "data": [...] }`. Le completion paginate usano `{ "data": [...], "links": {...}, "meta": {...} }`. I cataloghi modelli sono eccezioni con envelope dedicati. Gli SDK devono restituire oggetti di dominio, non costringere l'utente a rimuovere manualmente `data`.

## 8. Modello degli errori

L'envelope condiviso è:

```json
{
  "error": {
    "type": "conflict",
    "code": "turn_in_progress",
    "message": "A turn is already in progress for this chat.",
    "details": null
  }
}
```

Ogni SDK deve esporre una eccezione base `EnvaiError` con almeno:

- `http_status`
- `type`
- `code`, opzionale e nullable
- `message`
- `details`, opzionale e nullable
- eventuale request id restituito dall'infrastruttura

Sono consigliate sottoclassi o varianti per `AuthenticationError`, `AccessDeniedError`, `NotFoundError`, `ConflictError`, `ValidationError`, `RateLimitError`, `ProviderError`, `FileError` e `ServerError`.

L'applicazione deve poter decidere usando `code`, non analizzando `message`. Esempi importanti sono `turn_in_progress`, `no_turn_in_progress`, `tool_already_resolved`, `tool_timed_out` e `invalid_fork_destination`.

## 9. Retry e idempotenza

Gli SDK non devono ritentare automaticamente tutte le richieste.

Un retry automatico è ragionevole soltanto per operazioni già classificate retry-safe, principalmente `GET`, e per errori di rete avvenuti prima che qualsiasi byte della richiesta possa essere stato elaborato. Su `429`, `502`, `503` o `504` si applicano backoff esponenziale con jitter e `Retry-After`, ma lo status non rende retry-safe una mutazione.

I metodi `POST`, `PUT` e `DELETE` non devono essere ritentati automaticamente se il client non può dimostrare che il server non ha elaborato la richiesta. In particolare, creazione di risorse, invio di messaggi, generazione immagini, fork e restore possono produrre effetti duplicati.

`tool-output` è risolto una sola volta per `call_id` mentre il turno resta attivo. Durante il turno una seconda consegna restituisce `409`; al terminale il broker e i tombstone vengono eliminati, quindi lo stesso `call_id` restituisce `404`. Questo protegge la risoluzione server soltanto durante il turno, non l'effetto locale, e non rende il metodo retry-safe. Dopo un esito di rete ambiguo lo SDK può considerare riuscita la consegna soltanto se osserva il corrispondente `agent.tool.result`; altrimenti deve conservare uno stato ambiguo e non dedurre l'esito da un successivo `404`.

Lo stream usa `seq` come meccanismo di resume, non un retry della richiesta di messaggio.

## 10. Superficie degli endpoint

Tutti i path di questa sezione sono relativi al `base_url` che termina con `/api/v1`, salvo JWKS. I nomi indicati nelle tabelle sono la facciata pubblica language-neutral; il layer generato deve conservare gli `operationId` definiti in OpenAPI, così una rigenerazione non rinomina arbitrariamente le operazioni.

### 10.1 Esecuzione ATP

| Metodo | Path | Metodo SDK consigliato | Risultato |
| --- | --- | --- | --- |
| POST | `/chats/{chat_id}/messages` | `chats.send_message` | `ChatMessageAccepted` con `turn_id` |
| GET | `/chats/{chat_id}/events` | `chats.events` | stream asincrono di eventi ATP |
| POST | `/chats/{chat_id}/cancel` | `chats.cancel` | `TurnAccepted` |
| POST | `/chats/{chat_id}/steer` | `chats.steer` | `TurnAccepted` |
| POST | `/chats/{chat_id}/tool-output` | `chats.submit_tool_output` | nessun contenuto |
| POST | `/chats/{chat_id}/approval` | `chats.submit_approval` | non disponibile su alpha, risponde `409` |
| POST | `/chats/{chat_id}/fork` | `chats.fork` | coppia source e destination chat |
| POST | `/chats/{chat_id}/restore` | `chats.restore` | `RestoreChatAccepted` |
| GET | `/capabilities` | `capabilities.get` | versione protocollo, eventi e feature |

La risposta corrente di `capabilities` annuncia in `features`: `replay`, `fork`, `restore`, `ping`, `background_delegation` e `client_tools`; `steer` e `approval` sono assenti. `event_types` elenca esattamente i 17 tipi emessi dal gateway: `ping`, i cinque `turn.*` (incluso `turn.steered`), gli undici `agent.*` realmente emessi (`agent.started`, `agent.text.delta`, `agent.text.done`, `agent.delegated`, `agent.tool.start`, `agent.tool.complete`, `agent.tool.request`, `agent.tool.result`, `agent.context.compacting`, `agent.context.compacted`, `agent.done`) e nessun tipo approvazione. `families` contiene `turn` e `agent`.

Restano quindi due cose distinte: `events.json` dichiara 28 tipi evento e `capabilities.event_types` è la discovery positiva dei 17 tipi emessi adesso. Gli altri tipi (`agent.plan`, `agent.reasoning.delta`, `agent.reasoning.done`, `agent.tool.delta`, `agent.escalate`, `agent.escalation.result`, `agent.artifact`, `agent.failed` e i tre `approval.*`) validano contro lo schema ma lo stream corrente non li emette. Lo SDK deve usare `events.json` per il parsing tipizzato e le capabilities per abilitare le feature opzionali, senza rifiutare un evento valido perché non annunciato.

`POST /chats/{chat_id}/messages` risponde `503 server_error` con code `gateway_draining` mentre il gateway è in drain: nessun turno parte.

`POST /chats/{chat_id}/steer` richiede `text` non vuoto; `target_agent_id` deve essere un `ag_<ulid>` valido quando presente. Ometterlo o passare `null` indirizza l'orchestratore e l'evento `turn.steered` non contiene `target_agent_id`; un valore esplicito malformato o vuoto risponde `422 validation_error`.

### 10.2 Workspace e user tool

| Metodo | Path | Metodo SDK consigliato |
| --- | --- | --- |
| POST | `/workspaces` | `workspaces.create` |
| GET | `/workspaces` | `workspaces.list` |
| GET | `/workspaces/{workspace_id}` | `workspaces.get` |
| PUT | `/workspaces/{workspace_id}` | `workspaces.update` |
| POST | `/tools` | `tools.create` |
| GET | `/tools` | `tools.list` |
| GET | `/tools/{name}` | `tools.get` |
| PUT | `/tools/{name}` | `tools.update` |
| DELETE | `/tools/{name}` | `tools.delete` |

Un user tool è sempre `kind: "client"`. Il nome deve rispettare `^[a-z][a-z0-9_]{0,254}$`, essere unico per utente e non collidere con un tool di piattaforma riservato. `parameters` è un JSON Schema con root `object`. `timeout_seconds` ha default 120 e massimo 600.

La registrazione dell'utente non basta ad autorizzare il tool. `agent.external_tools_enabled` è un array di oggetti `AgentTool`, non un array di stringhe: ogni elemento richiede almeno `{ "name": "send_email" }` e può contenere proprietà aggiuntive. Il nome dell'user tool registrato deve corrispondere al campo `name` di uno di questi oggetti.

### 10.3 Chat e metadata completion

| Metodo | Path | Metodo SDK consigliato |
| --- | --- | --- |
| POST | `/workspaces/{workspace_id}/chats` | `chats.create` |
| GET | `/workspaces/{workspace_id}/chats` | `chats.list` |
| GET | `/workspaces/{workspace_id}/chats/{chat_id}` | `chats.get` |
| GET | `/workspaces/{workspace_id}/chats/{chat_id}/completions` | `completions.list` |
| GET | `/workspaces/{workspace_id}/chats/{chat_id}/completions/{completion_id}` | `completions.get` |

`completions.list` supporta i filtri `status`, `model_orchestrator`, `started_at[from]`, `started_at[to]` e `per_page`. Lo SDK deve offrire un iteratore/pager senza nascondere `links` e `meta` a chi li richiede.

### 10.4 Workspace files

| Metodo | Path | Metodo SDK consigliato | Risultato |
| --- | --- | --- | --- |
| GET | `/workspaces/{workspace_id}/files?path=...` | `files.read_or_list` | metadata, contenuto o directory entries |
| POST | `/workspaces/{workspace_id}/files` | `files.create` | file creato |
| PUT | `/workspaces/{workspace_id}/files` | `files.update` | file aggiornato |
| DELETE | `/workspaces/{workspace_id}/files?path=...&recursive=...` | `files.delete` | nessun contenuto |
| GET | `/workspaces/{workspace_id}/files/download?path=...` | `files.download` | byte stream |
| POST | `/workspaces/{workspace_id}/files/convert-to-pdf` | `files.convert_to_pdf` | byte stream PDF |

I path sono assoluti rispetto al workspace, iniziano con `/` e non possono contenere `..`, backslash, caratteri di controllo, slash duplicati o slash finale. Gli SDK devono usare percent-encoding corretto dei query parameter e non concatenare manualmente il path. Il contenuto testuale deve essere UTF-8 non vuoto, al massimo 1 MiB e 1.000 righe; `/MEMORY.md` ha inoltre un limite di 12.000 caratteri.

I download devono poter essere consumati come byte, stream o salvataggio su file senza caricare obbligatoriamente tutto in memoria.

### 10.5 Agenti

| Metodo | Path | Metodo SDK consigliato |
| --- | --- | --- |
| POST | `/agents` | `agents.create` |
| GET | `/agents` | `agents.list` |
| GET | `/agents/{agent_id}` | `agents.get` |
| PUT | `/agents/{agent_id}` | `agents.update` |
| DELETE | `/agents/{agent_id}` | `agents.delete` |

I campi principali sono `name`, `description`, `system_prompt`, `model`, `mode`, `tools_enabled`, `external_tools_enabled`, `permissions`, `can_write_knowledge`, `can_write_skills` e `config`.

`tools_enabled` riguarda capacità built-in. `external_tools_enabled` contiene oggetti `AgentTool` con almeno il campo `name`, riferiti ai client tool registrati dall'utente nel proprio registro (`user_tools`). Gli SDK non devono convertirli in stringhe né fondere i due array.

### 10.6 Team

| Metodo | Path | Metodo SDK consigliato |
| --- | --- | --- |
| POST | `/teams` | `teams.create` |
| GET | `/teams` | `teams.list` |
| GET | `/teams/{team_id}` | `teams.get` |
| PUT | `/teams/{team_id}` | `teams.update` |
| DELETE | `/teams/{team_id}` | `teams.delete` |

Un team richiede `name` e `orchestrator_id`. `members` associa agenti membri e configurazione di delegazione. Lo SDK deve preservare l'ordine dei membri fornito dal server.

### 10.7 Modelli e API key

| Metodo | Path | Metodo SDK consigliato |
| --- | --- | --- |
| GET | `/models` | `models.list` |
| POST | `/api-keys` | `api_keys.create` |
| GET | `/api-keys` | `api_keys.list` |
| DELETE | `/api-keys/{key_id}` | `api_keys.revoke` |

Il catalogo modelli restituisce `{object, data, defaults}`. Gli SDK devono modellare i default per ruolo e i campi di deprecazione senza assumere che il primo elemento sia il default.

Il token plaintext di una API key è presente soltanto nella risposta di creazione. Il tipo restituito da `create` deve renderlo evidente e la documentazione deve invitare a salvarlo subito in uno storage sicuro.

### 10.8 Immagini

| Metodo | Path | Metodo SDK consigliato |
| --- | --- | --- |
| POST | `/images/generations` | `images.generate` |
| GET | `/images/generations/{record}` | `images.get_generation` |
| POST | `/images/uploads` | `images.upload` |
| GET | `/images/models` | `images.list_models` |

La generazione è asincrona e risponde `202`. `images.generate_and_wait` può essere un helper opzionale implementato come polling del record, con timeout, cancellazione e backoff configurabili. Non deve essere il solo metodo disponibile.

`input_image` e `input_images` sono mutuamente esclusivi. Mask e objects richiedono `input_image`; sono ammesse al massimo 10 immagini e 50 MiB aggregati. Un eventuale `webhook_url` deve essere HTTPS pubblico e non può puntare a loopback, reti private o metadata endpoint. Lo SDK può validare questi vincoli localmente per feedback rapido, mantenendo comunque autorevole la validazione server.

L'upload è multipart, accetta PNG, JPEG, WebP o GIF fino a 10 MiB e produce un upload temporaneo con scadenza. Lo SDK deve accettare byte, file path o stream secondo le convenzioni del linguaggio e deve impostare correttamente filename e content type.

### 10.9 Endpoint infrastrutturali

| Metodo | Path | Autenticazione | Uso SDK |
| --- | --- | --- | --- |
| GET | `/up` | pubblica | health del control plane |
| GET | `/.well-known/jwks.json` | pubblica | verifica JWT e client tool |
| GET | `/artifacts/download` | URL firmato | download artifact |
| POST | `/webhooks/providers/{provider}` | firma provider | endpoint server-to-server, non metodo client ordinario |
| POST | `/tests/images/generate` | solo ambiente locale | helper di test, da escludere dal client pubblico stabile |

Il download artifact usa una URL firmata con `path`, `expires` e `signature`. Lo SDK deve offrire un helper che scarica una URL già firmata, senza tentare di rigenerarne la firma.

## 11. Agent Team Protocol

### 11.1 Trasporto

Lo stream canonico è SSE:

```http
GET /api/v1/chats/{chat_id}/events?after=<seq>
Accept: text/event-stream
Authorization: Bearer <api-key>
```

Ogni frame usa una riga `data:` contenente un oggetto JSON ATP. Non esiste il terminatore OpenAI `[DONE]`. La connessione rimane attiva e riceve anche turni futuri della stessa chat finché viene chiusa dal client o dall'infrastruttura.

Il parser deve rispettare la grammatica SSE: accumulare più righe `data:`, terminare l'evento sulla riga vuota, ignorare commenti e campi sconosciuti e gestire chunk di rete che dividono una singola riga o più eventi nello stesso chunk.

### 11.2 Envelope

Ogni evento ha questa forma:

```json
{
  "v": 2,
  "id": "evt_<ulid>",
  "seq": 42,
  "ts": "2026-08-17T14:00:00Z",
  "chat_id": "cht_<ulid>",
  "turn_id": "cmpl_<ulid>",
  "agent": {
    "instance_id": "ag_<ulid>",
    "name": "researcher",
    "role": "member",
    "parent_id": "ag_<ulid>"
  },
  "type": "agent.text.delta",
  "data": {"delta":"..."}
}
```

`turn_id` è `null` per `ping`. `agent` è `null` per gli eventi `turn.*`, `ping` e `approval.response`. I valori `seq` positivi sono l'unica autorità di ordinamento e sono monotoni per chat, continui attraverso più turni. Il `ping` wire-only usa `seq: 0` e non partecipa al replay.

Lo SDK deve controllare `v`. Una versione non supportata deve produrre un errore di protocollo chiaro prima di interpretare il payload.

### 11.3 Famiglie di evento

Gli eventi di turno sono:

- `turn.started`
- `turn.completed`
- `turn.cancelled`
- `turn.failed`
- `turn.steered`
- `ping`

Gli eventi agente sono:

- `agent.started`
- `agent.plan`
- `agent.text.delta`
- `agent.text.done`
- `agent.reasoning.delta`
- `agent.reasoning.done`
- `agent.delegated`
- `agent.tool.start`
- `agent.tool.delta`
- `agent.tool.complete`
- `agent.tool.request`
- `agent.tool.result`
- `agent.escalate`
- `agent.escalation.result`
- `agent.artifact`
- `agent.context.compacting`
- `agent.context.compacted`
- `agent.done`
- `agent.failed`

Gli eventi approvazione sono:

- `approval.request`
- `approval.response`
- `approval.expire`

La tassonomia sopra è quella dichiarata da `events.json`, 28 tipi in totale. Il gateway corrente ne emette 17: `ping`, `turn.started`, `turn.completed`, `turn.cancelled`, `turn.failed`, `turn.steered`, `agent.started`, `agent.text.delta`, `agent.text.done`, `agent.delegated`, `agent.tool.start`, `agent.tool.complete`, `agent.tool.request`, `agent.tool.result`, `agent.context.compacting`, `agent.context.compacted`, `agent.done`. Gli SDK devono accettare anche i tipi dichiarati ma non emessi, perché additive e già schema-validi.

Un evento con `type` sconosciuto deve essere restituito come `UnknownEvent` con envelope e JSON `data` intatti. Non deve interrompere lo stream. Nuovi tipi sono additive; una rottura dell'envelope incrementa `v`.

### 11.4 Evento terminale di turno

Il turno termina con esattamente uno tra `turn.completed`, `turn.cancelled` e `turn.failed`. `agent.done` non è sufficiente per considerare chiuso il turno, perché possono esserci più agenti e più branch.

Gli helper ad alto livello come `run_turn` o `send_and_wait` devono attendere l'evento terminale con lo stesso `turn_id` restituito da `send_message`.

`turn.completed`, `turn.cancelled` e `turn.failed` contengono usage e costo del turno, oltre ai totali informativi della chat (`chat_usage` e `chat_cost`). I totali della chat non sono un budget cumulativo e non devono essere usati dallo SDK per bloccare nuovi messaggi.

Lo snapshot iniziale registrava una divergenza oggi risolta: `chat_usage` poteva omettere `duration_ms` e il terminale generato dall'interruzione poteva contenere un `usage` vuoto. Nel runtime corrente ogni evento che porta usage (`agent.done`, `agent.tool.complete`, `turn.completed`, `turn.cancelled`, `turn.failed`) emette un `Usage` completo, con `duration_ms` sempre presente e l'usage dei tool normalizzato. Gli SDK devono comunque fallire chiuso su un payload noto non valido, restituendo il JSON originale invece di inventare valori; i test in `apps/execution-plane/internal/atp` validano gli eventi serializzati del gateway contro `events.json`.

### 11.5 Ricostruzione del testo

Il testo incrementale arriva con `agent.text.delta`. `agent.text.done` contiene il testo completo di quella istanza agente.

L'output user-facing è quello dell'orchestratore. Gli SDK possono esporre tutti i delta, ma gli helper che restituiscono una sola risposta devono aggregare soltanto l'istanza con `role: "orchestrator"`, salvo opzione esplicita diversa.

I delta di subagent, reasoning e tool non devono essere concatenati al testo finale dell'orchestratore.

### 11.6 Delegazione

`agent.delegated` è emesso dal parent e contiene `child_instance_id`, `child_name`, `mode`, `title` e `task`. Il corrispondente `agent.started` del figlio porta `parent_id` uguale all'istanza parent. Ogni `agent.started` è chiuso da un solo `agent.done` per quella istanza; un agente completato emette `agent.text.done` prima del proprio `agent.done`, i child chiudono prima del root e il root prima del terminale di turno.

In modalità foreground il parent si sospende fino al completamento del figlio. In modalità background gli eventi parent e child possono interlecciarsi. Lo SDK non deve assumere che gli eventi di un agente siano contigui.

### 11.7 Tool di piattaforma

I tool `server`, `knowledge` e `code` usano:

```text
agent.tool.start
agent.tool.delta, opzionale
agent.tool.complete
```

Il campo `kind` distingue il luogo di esecuzione. Gli SDK devono preservare i payload strutturati di `result` e non convertirli sempre in stringa.

### 11.8 Usage e costo

`Usage` include almeno:

```json
{
  "input_tokens": 1234,
  "output_tokens": 567,
  "cost_usd": 0.42,
  "duration_ms": 8123,
  "sprite_ms": 1500
}
```

`cost_usd` è un numero decimale in USD. Nei linguaggi che distinguono `float` e decimal, il modello pubblico deve evitare arrotondamenti a centesimi. Lo SDK non deve ricalcolare il costo da tariffe locali: il valore autorevole arriva dal server.

`input_tokens`, `output_tokens`, `cost_usd` e `duration_ms` sono obbligatori; `sprite_ms`, `sprite_ram_gb_ms` e `tool_calls` sono opzionali e le chiavi estensione restano ammesse. Il terminale di turno riporta il totale del turno con l'usage di ogni agente contato una volta; `agent.done` porta l'usage proprio dell'istanza, root e child separati. `chat_usage` e `chat_cost` sono i totali residenti della chat, informativi e senza budget cumulativo, e `chat_usage.duration_ms` somma le durate dei turni senza idle. I totali residenti si azzerano alla reidratazione o eviction della chat: non sono una base di billing durevole.

## 12. Replay, riconnessione e consumo concorrente

Il client conserva l'ultimo `seq` consegnato con successo all'applicazione. Dopo una breve disconnessione, durante la stessa istanza runtime della chat, riapre lo stream con `after=<last_seq>` e riceve gli eventi trattenuti strettamente successivi.

Il replay non è durevole né illimitato nello snapshot `alpha`: il buffer per chat conserva in memoria gli ultimi 10.000 eventi e non viene persistito. Dopo eviction o restart il gateway idrata soltanto il contesto conversazionale, ricrea la chat con `nextSeq: 1` e non segnala un cambio di epoch. Un checkpoint elevato può quindi non ricevere più eventi, mentre uno antico può ricevere una coda incompleta. Il protocollo non espone `cursor_expired`, epoch o ultimo cursore disponibile; nessun algoritmo SDK può garantire resume corretto attraverso eviction o restart.

Durante la stessa istanza runtime, ogni subscriber live ha un buffer di 256 eventi e il fan-out scarta silenziosamente un evento quando è pieno. Lo SDK può rilevare un salto verso l'alto e tentare replay, ma non può distinguere in modo affidabile un reset della sequenza dopo reidratazione. La continuità dichiarata vale quindi solo finché la chat runtime rimane residente.

L'algoritmo raccomandato è:

1. Aprire lo stream con il cursore iniziale, default `0`.
2. Decodificare e validare l'envelope; gestire `ping` separatamente perché è wire-only e usa `seq: 0`.
3. Ignorare eventuali duplicati non-ping con `seq <= last_seq`.
4. Se un evento non-ping ha `seq > last_seq + 1`, non consegnarlo: chiudere la connessione e tentare un replay con `after=last_seq`.
5. Se anche il replay inizia oltre `last_seq + 1`, sollevare un `ReplayGapError` con il range mancante. Il client non può ricostruire autonomamente eventi già espulsi.
6. Consegnare l'evento all'applicazione.
7. Aggiornare `last_seq` solo dopo la consegna riuscita.
8. In caso di errore transient, riconnettere con backoff e jitter usando `after=last_seq`, sapendo che questa è una garanzia best-effort limitata alla stessa runtime instance.
9. Se l'applicazione sa che è avvenuta eviction o restart, non riutilizzare silenziosamente il checkpoint: richiedere una policy esplicita, per esempio riaprire da zero e trattare gli eventi come una nuova epoch oppure fallire chiedendo riconciliazione applicativa.
10. Interrompere su autenticazione fallita, versione protocollo incompatibile, cancellazione locale o errore non transient.

Un singolo oggetto stream non deve essere consumato contemporaneamente da più coroutine o goroutine. Se lo SDK offre callback, iteratori e helper di aggregazione, deve avere un solo reader interno e fare fan-out in modo controllato.

`ping` mantiene viva la connessione, non appartiene a un turno, usa `seq: 0` e non modifica mai il cursore applicativo. Lo SDK può consegnarlo come evento di controllo o filtrarlo, ma può usarlo per confermare che la subscription live del gateway è stata registrata.

## 13. Flusso consigliato per un turno

Per evitare la race tra messaggio ed eventi, l'helper alto livello deve aprire o mantenere lo stream prima di inviare il messaggio. L'apertura e la lettura dello stream devono avvenire in una task concorrente: il metodo non deve attendere un evento di turno prima di inviare il messaggio, perché su una chat idle il primo evento applicativo arriverà proprio dal nuovo turno.

Lo snapshot `alpha` ha però una limitazione server: `Chat.Stream` invia gli header HTTP, esegue il replay e soltanto dopo registra la subscription live. Un evento emesso tra replay e subscription può quindi essere perso. Lo SDK non può eliminare questa finestra usando solo `after`; non deve considerare il solo `200 OK` come prova che la subscription sia attiva. Come workaround transitorio, su un nuovo stream deve attendere il primo `ping`, che viene emesso dopo la registrazione live, prima di inviare il messaggio. Questo può aggiungere fino a un intervallo heartbeat. La correzione definitiva è rendere atomici replay e subscribe nel gateway; finché non viene applicata, gli helper non devono promettere attach senza perdita in ogni interleaving.

Pseudo-codice language-neutral:

```text
stream = client.chats.events(chat_id, after=checkpoint)
stream.start_in_background()
await stream.live_subscription_confirmed_by_ping
accepted = client.chats.send_message(chat_id, message)

for event in stream:
    if event.type != ping:
        checkpoint = max(checkpoint, event.seq)
    if event.turn_id != accepted.turn_id:
        continue
    emit(event)
    if event.type in [turn.completed, turn.cancelled, turn.failed]:
        return TurnResult.from(event, orchestrator_text)
```

La API di basso livello deve lasciare disponibili separatamente `events` e `send_message`. La API di alto livello può esporre `send_and_stream` e `send_and_wait`, ma non deve nascondere i singoli eventi.

## 14. Client tool

### 14.1 Registrazione e visibilità

Il chiamante registra il tool nel proprio account (`POST /api/v1/tools`), condiviso tra tutti i suoi workspace, e inserisce `{ "name": "<tool_name>" }` in `external_tools_enabled` della definizione agente. Il gateway espone il tool al modello soltanto se entrambe le condizioni sono vere.

### 14.2 Richiesta firmata

Quando un agente chiama un client tool, lo stream emette:

```json
{
  "type": "agent.tool.request",
  "data": {
    "call_id": "tc_<ulid>",
    "tool_name": "send_email",
    "kind": "client",
    "args": {"to":"user@example.com"},
    "signature": {
      "alg": "EdDSA",
      "kid": "sig_<ulid>",
      "sig": "base64url_without_padding"
    }
  }
}
```

Prima di eseguire il tool lo SDK deve verificare la firma. Il payload firmato è la concatenazione byte UTF-8:

```text
call_id + "\n" + tool_name + "\n" + canonical_json(args)
```

`canonical_json` segue RFC 8785. Non è sufficiente ordinare superficialmente le chiavi: numeri, Unicode, escaping e oggetti annidati devono avere una implementazione conforme.

La chiave pubblica si seleziona da `/.well-known/jwks.json` usando `kid`. Lo SDK deve mantenere una cache JWKS con TTL e, quando il `kid` non è presente o la verifica fallisce dopo una rotazione, aggiornare la JWKS una volta prima di rifiutare.

Una richiesta con algoritmo diverso da `EdDSA`, firma non valida, `kid` sconosciuto dopo refresh o payload non canonicalizzabile deve fallire senza eseguire il tool.

### 14.3 Registro locale e validazione

Lo SDK deve offrire un registro locale che includa handler, schema e timeout:

```text
register_tool(name, handler, parameters_schema, timeout)
```

Quando riceve `agent.tool.request`, deve verificare firma e nome e validare `args` contro lo schema locale. L'evento non trasporta la deadline assoluta né il timeout residuo: il timer server è già partito prima della firma e dell'emissione. Il runner deve quindi usare un margine conservativo inferiore al `timeout_seconds`, cancellare il lavoro quando osserva `agent.tool.result` o l'evento terminale del turno e richiedere handler idempotenti o compensabili per operazioni con effetti esterni.

Una firma valida prova origine e integrità, non freschezza. Prima di eseguire il handler, il runner deve consultare un registro durevole o almeno persistibile indicizzato da `(chat_id, call_id)`. Le richieste già in esecuzione o già risolte non devono rieseguire il side effect dopo replay o riconnessione. Lo SDK deve registrare lo stato prima dell'invocazione, associare `agent.tool.result` alla chiamata e permettere all'applicazione di riconciliare uno stato ambiguo dopo crash.

Un tool non registrato localmente deve produrre un output di errore controllato, non essere ignorato lasciando il branch sospeso.

### 14.4 Consegna del risultato

Il risultato viene inviato a:

```http
POST /api/v1/chats/{chat_id}/tool-output
Content-Type: application/json
Authorization: Bearer <api-key>

{
  "call_id":"tc_<ulid>",
  "output":"{\"sent\":true}",
  "is_error":false
}
```

`output` è sempre una stringa. Se il handler restituisce un valore strutturato, lo SDK deve serializzarlo una volta in JSON e inviare quella stringa. Il body HTTP viene poi serializzato normalmente, perciò nel wire JSON la stringa appare escaped. Il limite corrente è 128 KiB.

Il successo risponde `204 No Content` ed è confermato sullo stream da `agent.tool.result`. Un timeout server produce `agent.tool.result` con `is_error: true` e output contenente `tool_timeout`. Un risultato arrivato tardi riceve `409 tool_timed_out`.

Il client tool runner deve limitare la concorrenza, propagare la cancellazione locale e non registrare nei log argomenti o output sensibili per default.

## 15. Approval

Il modello ATP prevede due passaggi. Un subagent emette `agent.escalate`; l'orchestratore decide se risolvere internamente o emettere `approval.request` verso l'utente. La risposta viene inviata a `/chats/{chat_id}/approval`, poi lo stream emette `approval.response` e l'orchestratore può inviare `agent.escalation.result` al subagent.

Su `alpha` l'endpoint HTTP è registrato e autenticato ma la risoluzione è ancora uno stub che restituisce `409`. Gli SDK devono includere i tipi evento per compatibilità col protocollo, ma `submit_approval` deve essere marcato come non disponibile nello snapshot corrente e non deve essere usato da helper automatici finché `GET /capabilities` non annuncia una feature effettivamente utilizzabile.

Le applicazioni devono fallire chiuso alla scadenza indicata da `deadline` e non approvare automaticamente operazioni pericolose.

## 16. Steering, cancellazione, restore e fork

`steer` è definito dal protocollo per inserire indicazioni nel turno attivo al prossimo tool boundary. Senza `target_agent_id` (o con `null`) è diretto all'orchestratore e l'evento emesso non contiene il campo; un `target_agent_id` esplicito deve essere un `ag_<ulid>` valido e un valore malformato o vuoto produce `422 validation_error`. Se non esiste un turno attivo il server risponde `409 no_turn_in_progress`. Nello snapshot `alpha`, però, l'implementazione Go si limita a validare il turno ed emettere `turn.steered`: l'iniezione effettiva nel loop LLM è ancora fuori scope e `steer` non è annunciato in `capabilities.features`. Gli SDK devono esporre il primitive e l'evento, ma non promettere che il testo modifichi l'esecuzione finché il gateway non implementa la consegna.

`cancel` annulla il turno attivo e propaga la cancellazione ai subagent. Il completamento osservabile è `turn.cancelled`, non la sola risposta HTTP `202`.

`restore` sostituisce il contesto persistito della chat con `orchestrator_context`. Non apre un turno e restituisce `turn_id: null`. Il primo elemento deve essere il contesto compattato; gli elementi successivi sono messaggi assistant dell'orchestratore. I subagent non vengono ripristinati.

L'idratazione automatica corrente è fail-open: un errore DB o JSON corrotto può degradare a cronologia vuota. Anche le scritture del contesto sono asincrone, best-effort e limitate nel tempo; un errore non fa fallire il turno e una successiva reidratazione può quindi usare storia assente o obsoleta. La persistenza conserva ruolo, contenuto e marker di compaction, ma non tutta la fedeltà delle precedenti tool call. Un SDK non può rilevare questi casi dal solo stream. Per una garanzia forte il gateway deve rendere persistenza e caricamento osservabili e fail-closed; fino ad allora gli SDK non devono promettere che ogni reidratazione preservi integralmente il contesto.

`fork` copia la cronologia da una chat sorgente a una chat destinazione. Lo SDK deve prima creare la destinazione sul control plane con il team desiderato, poi chiamare il fork sulla sorgente. Sorgente e destinazione devono essere idle; la destinazione deve appartenere allo stesso utente e workspace ed essere vuota. Un turno attivo sulla sorgente produce `409`. Dopo il fork le due chat evolvono indipendentemente.

Un helper `fork_chat` può orchestrare i due passaggi, ma deve restituire anche la chat destinazione creata e non deve cancellarla automaticamente se il secondo passaggio fallisce senza una opzione esplicita.

## 17. Struttura consigliata degli SDK

Ogni implementazione deve separare il codice generabile dal runtime ATP scritto a mano.

### 17.1 Moduli pubblici

- `EnvaiClient`: configurazione, lifecycle e accesso ai service
- `WorkspacesService`
- `UserToolsService`
- `ChatsService`
- `CompletionsService`
- `WorkspaceFilesService`
- `AgentsService`
- `TeamsService`
- `ModelsService`
- `ApiKeysService`
- `ImagesService`
- `CapabilitiesService`
- `EventStream`
- `ClientToolRegistry` e `ClientToolRunner`
- modelli ed errori condivisi

### 17.2 Parte generata

OpenAPI può generare modelli request/response, service REST e serializzazione. La generazione deve usare `packages/contracts/openapi.yaml` insieme a `packages/contracts/events.json`, risolvendo gli external `$ref`. `x-event-schema` è un'estensione custom: il bundle Redocly corrente può incorporare `Event` senza copiare tutti i relativi `$defs`, quindi non deve essere usato per la code generation senza una verifica specifica del bundle; in alternativa il generatore deve ricevere entrambi i file o una import mapping esplicita.

Il codice generato non deve contenere la logica completa per SSE, riconnessione, unknown event, verifica EdDSA/RFC 8785, tool registry, fan-out, aggregazione del testo o `send_and_wait`. Questi elementi devono restare in un runtime scritto a mano e coperto da test cross-language.

### 17.3 API sincrona e asincrona

Python deve offrire client sync e async, condividendo modelli e semantica. TypeScript usa primitive async native. Go deve usare `context.Context`, channel o iteratori espliciti e chiusura deterministica.

Le differenze idiomatiche sono ammesse, ma nomi logici, default, error code e risultato devono rimanere equivalenti.

### 17.4 Configurazione comune

Il costruttore deve supportare almeno:

- `api_key`
- `base_url`
- `jwks_url` opzionale, altrimenti derivato dall'origine del `base_url`
- timeout HTTP ordinario
- timeout di connessione stream
- policy di retry
- numero massimo di riconnessioni SSE o durata massima
- trasporto HTTP personalizzato
- logger opzionale con redazione dei segreti
- cache JWKS personalizzabile
- identificativo e versione SDK

## 18. Helper di alto livello

Gli SDK possono offrire helper senza sostituire i primitive di basso livello:

- `send_and_stream(chat_id, message)` apre lo stream, invia il messaggio e restituisce gli eventi del turno
- `send_and_wait(chat_id, message)` restituisce testo orchestratore, evento terminale, usage e costo
- `run_client_tools(stream, registry)` verifica ed esegue richieste client tool
- `generate_and_wait(...)` attende una generazione immagine
- `download_to_file(...)` salva file e artifact in streaming
- `iterate_completions(...)` pagina automaticamente

Ogni helper deve accettare cancellazione e timeout. Deve anche restituire abbastanza metadata da diagnosticare il turno, in particolare `chat_id`, `turn_id`, ultimo `seq` ed evento terminale.

## 19. Sicurezza

Gli SDK devono applicare queste regole:

- mai loggare API key, JWT, header `Authorization` o URL firmate complete
- verificare ogni client tool prima dell'esecuzione
- non eseguire tool sconosciuti o con firma invalida
- applicare timeout e limiti di concorrenza ai tool locali
- non seguire redirect cross-origin mantenendo `Authorization`
- validare path e URL di download senza permettere path traversal locale
- offrire scrittura atomica opzionale per i download su file
- redigere argomenti e output tool nei log per default
- non trattare `reasoning` come testo user-facing o come contenuto sempre disponibile
- preservare gli errori di autorizzazione senza trasformarli in not found locali, salvo il comportamento fail-closed restituito dal server

## 20. Osservabilità

Lo SDK deve permettere hook non invasivi per:

- inizio e fine richiesta HTTP
- retry e riconnessione
- apertura e chiusura stream
- evento ricevuto, usando solo tipo, id, seq, chat e turn id per default
- client tool iniziato e terminato, senza payload sensibile
- durata e status code

Gli header di tracing standard possono essere propagati se presenti. L'SDK non deve generare log per ogni delta testuale a livello informativo.

## 21. Compatibilità e versionamento

ATP usa `v` nell'envelope. Un nuovo campo opzionale o un nuovo tipo evento è compatibile. Una modifica incompatibile incrementa `v`.

Gli SDK devono:

- ignorare tipi evento sconosciuti mantenendoli accessibili
- conservare campi sconosciuti in una mappa `extra` quando il linguaggio lo consente
- rifiutare una versione envelope non supportata
- usare `GET /capabilities` per feature opzionali
- non dedurre supporto soltanto dalla versione del package SDK

La versione dello SDK segue SemVer. L'aggiunta del supporto a un evento ATP additive è normalmente minor. La rimozione o modifica di una API pubblica SDK è major.

## 22. Suite di conformità cross-language

Ogni SDK deve superare gli stessi fixture test.

### 22.1 HTTP

- aggiunta corretta di base URL, prefisso e bearer token
- decoding di resource, collection, paginazione, `204` e binari
- mapping completo dell'envelope Error
- nessun retry automatico non sicuro sui metodi mutanti
- rispetto di `Retry-After`
- multipart upload e download streaming

### 22.2 SSE

- evento diviso tra più chunk TCP
- più eventi nello stesso chunk
- più righe `data:`
- commenti e campi SSE sconosciuti
- JSON non valido
- versione ATP non supportata
- tipo evento sconosciuto
- riconnessione con `after=last_seq`
- deduplicazione su `seq`
- rilevamento di un gap live con replay di recupero
- `ReplayGapError` quando il cursore è precedente al buffer trattenuto
- checkpoint precedente a eviction o restart con reset della sequenza
- evento emesso nella finestra tra replay e subscribe
- overflow del buffer subscriber
- `ping` con `seq: 0` escluso dal cursore
- cancellazione locale
- interleaving di parent e subagent
- identificazione dell'unico evento terminale del turno

### 22.3 Client tool

- verifica Ed25519 valida
- `kid` non presente seguito da refresh JWKS
- firma invalida senza esecuzione handler
- canonicalizzazione RFC 8785 con oggetti annidati, Unicode e numeri
- tool locale sconosciuto
- timeout locale e server
- serializzazione stringa dell'output strutturato
- limite 128 KiB
- `409 tool_already_resolved`
- `409 tool_timed_out`
- `404` per lo stesso `call_id` dopo il terminale del turno
- concorrenza limitata tra più tool request
- replay di una request già in esecuzione o risolta senza rieseguire il side effect
- cancellazione locale quando arriva un risultato server o un terminale di turno

### 22.4 Flussi end-to-end simulati

- chat creata, stream aperto, messaggio inviato, delta e `turn.completed`
- tentativo di secondo messaggio durante turno attivo con `409 turn_in_progress`
- cancel seguito da `turn.cancelled`
- steer con e senza target
- restore senza apertura turno
- fork verso chat appena creata
- riconnessione breve nella stessa runtime instance, con deduplicazione e rilevamento dei gap osservabili
- errore o policy esplicita quando il resume attraversa eviction o restart

Le fixture JSON ATP devono essere condivise in un package del repository per evitare divergenze tra linguaggi.

## 23. Stato reale di alpha e limitazioni note

Lo snapshot analizzato espone 52 operazioni HTTP nel contratto combinato e usa ATP envelope versione 2.

Sono già presenti:

- modello chat senza run
- turni multipli e un solo turno attivo per chat
- SSE con replay `after`
- sequenza continua tra turni soltanto mentre la chat runtime resta residente
- cancel completo con grace di finalizzazione di cinque secondi e steer disponibile come controllo/evento, con iniezione LLM ancora non implementata e feature non annunciata
- usage completo e obbligatorio su ogni evento che lo porta, con `duration_ms` sempre presente
- graceful shutdown con drain (`503 gateway_draining` sui nuovi messaggi) e attesa della finalizzazione prima dell'uscita
- persistenza del contesto, con idratazione corrente fail-open in caso di errore
- compaction events con metriche opzionali tipizzate
- fork e restore
- client tool registrati per utente, condivisi tra i workspace del proprietario
- firma EdDSA e JWKS
- timeout server e risoluzione at-most-once dei client tool, senza deduplicazione automatica degli effetti locali
- completion metadata-only con usage e costo
- workspace files e conversione PDF

Lo snapshot iniziale analizzato registrava due divergenze oggi risolte: i terminali Go potevano omettere `duration_ms` in `chat_usage` e `GET /capabilities` non elencava in modo completo i tipi emessi. Il runtime corrente emette usage completo e la risposta di capabilities elenca esattamente i 17 tipi emessi, quindi va usata come discovery positiva e non come allowlist del parser: i tipi dichiarati ma non emessi (plan, reasoning, escalation, artifact, approval) restano validi in `events.json`.

La risoluzione approval è dichiarata dal protocollo ma l'endpoint Go è ancora uno stub `409` e i tipi `approval.*` non sono annunciati. L'attach SSE presenta inoltre una finestra tra replay e registrazione della subscription live; il workaround basato su `ping` riduce la race per i nuovi turni ma non sostituisce una subscribe atomica. Replay, checkpoint e sequence non sopravvivono a eviction o restart.

Durante l'analisi il documento ATP narrativo conteneva alcuni nomi legacy `chat.turn.*`, mentre schema, OpenAPI, codice e test usavano `turn.*`. La nomenclatura da implementare negli SDK è `turn.*`; il documento ATP è stato corretto nello stesso intervento per riallineare prefisso pubblico, versione dell'envelope, payload terminali, client tool, replay ed esempi agli schemi machine-readable. `events.json` è stato precisato affinché `ping` sia l'unico evento ammesso con `seq: 0`, `turn_id: null` e `agent: null`.

Sono state inoltre corrette nel contratto OpenAPI tre divergenze osservate nell'implementazione Laravel: `404` su creazione di un workspace tool per workspace inesistente, `422` su eliminazione di un team ancora associato a chat e l'envelope reale `too_many_requests` delle risposte `429`. Sono stati anche convertiti i `nullable` legacy nella sintassi OpenAPI 3.1, separando il wire SSE testuale dallo schema evento indicato da `x-event-schema`, e corretti gli esempi ULID non validi. Il contratto risultante supera la validazione Redocly; le correzioni sono parte della base normativa per la generazione degli SDK.

## 24. Checklist per il primo SDK

Prima di iniziare le implementazioni nei diversi linguaggi occorre stabilizzare questi artefatti:

1. (risolto) I terminal event Go validano contro `events.json`; i test in `internal/atp` compilano lo schema e validano gli eventi serializzati di replay e SSE reali.
2. Rendere durevoli epoch, cursore ed eventi oppure specificare e segnalare esplicitamente `cursor_expired` dopo retention, eviction e restart.
3. Rendere atomici replay e subscribe nel gateway, aggiungendo un test concorrente che impedisca perdite durante l'attach SSE.
4. Rendere l'idratazione fail-closed quando contesto persistito o JSON non sono leggibili.
5. (risolto) `GET /capabilities` elenca esattamente i 17 tipi evento emessi e le sei feature disponibili; mantenerlo allineato quando cambia l'unione emessa.
6. Validare entrambi i file machine-readable in CI, compresi gli external `$ref`; la suite `internal/atp` copre già il lato eventi (jsonschema v6 test-only e `AssertFormat` su `events.json#/$defs/Event`).
7. Pubblicare fixture ATP e HTTP condivise.
8. Definire il comportamento ufficiale della capability approval finché l'endpoint è uno stub.
9. Scegliere il primo SDK di riferimento e usare i suoi test di conformità come modello, senza copiarne dettagli idiomatici negli altri linguaggi.
10. Generare soltanto modelli e primitive REST, mantenendo il runtime SSE e client tool esplicito.
11. Verificare la compatibilità dell'RFC 8785 scelta in TypeScript, Python e Go usando gli stessi vettori di firma.
12. Documentare esempi equivalenti per creazione workspace, creazione chat, streaming turno e client tool.

Il primo SDK è conforme quando un'applicazione può configurare le risorse, aprire una chat, consumare gli eventi ATP senza nascondere violazioni di protocollo, riprendere best-effort lo stream entro la retention della stessa runtime instance, rilevare i gap osservabili e invocare automaticamente al massimo una volta un handler client-tool per `(chat_id, call_id)`. Un crash tra side effect e registrazione durevole resta ambiguo: l'applicazione deve usare idempotency key, transazioni o riconciliazione esplicita. La conformità piena con resume durevole resta bloccata dalle limitazioni server elencate sopra.
