# Chat attachments

Native chat-file uploads require contract-3 capabilities advertising `chat_attachments` and `chat_attachment_originals`. The SDK checks both before the multipart POST and never falls back to OCR or sends the text alone. Other attachment reads, retries and deletes, and messages containing attachment IDs, still require only `chat_attachments`.

Capabilities accept the canonical homogeneous string list or a strict string-to-boolean map. The map is normalized to enabled names on `Capabilities.features: list[str]`, preserving the existing public type. Mixed lists, non-boolean maps and absent names fail closed before mutation.

Upload one immutable original with an upload idempotency key. A native upload becomes ready without conversion. Its conversion status is `not_required` and `derived` is `None`; `wait()` returns only when that status is paired with lifecycle `ready` and keeps polling otherwise. Legacy `completed`, terminal `failed`, recoverable failures and withdrawn resources retain their existing behavior. Send ordered attachment IDs with a separate stable message idempotency key. A message acknowledgment must contain those same IDs. Mutations are never automatically retried, even with a key: reconcile an uncertain response before explicitly replaying the same payload and key. Native retries return `409 attachment_retry_not_permitted` and the SDK does not resubmit them.

Native chat uploads accept `.md`, `.txt`, `.csv`, `.pdf`, `.mp3`, and `.mp4`, retain their originals without OCR, transcription, or document conversion, and deliver them as model-native content parts. New native uploads reject `.xlsx` and `.docx`, while existing legacy converted records for `.md`, `.txt`, `.csv`, `.xlsx`, `.pdf`, `.docx`, `.mp3`, and `.mp4` remain usable. In the legacy conversion path, PDF/DOCX used Mistral OCR, MP3 and the mixed audio tracks of MP4 used direct ElevenLabs Scribe without analyzing MP4 visual frames, and converted text had no model-specific multimodal routing. There is no automatic Wiki indexing, guaranteed Office editing/export, or automatic mounting of original files in tools.

For legacy conversions, the complete textual projection must fit the server and model context budgets; oversized input fails explicitly and the converted attachment stays available. No silent truncation occurs. Originals and derivatives remain private and scoped to the owning user, workspace and chat. Forked/restored text does not grant access to originals. Status/list/download/retry/delete are separate operations. Download selects `kind=original` or `kind=derived`.

Draft retention defaults to 24 hours; accepted attachments and input snapshots default to 14 days. Idempotent replay is guaranteed while the retained input snapshot exists. Withdrawal blocks new reads immediately; physical cleanup may wait for live conversion leases. Deletion does not erase already accepted conversation content.

Conversion cost is exposed in the attachment's `usage_cost`; inference is attributed to the turn. Missing tariffs and uncertain outcomes stay pending with null totals. An explicit conversion retry creates a linked accounting cycle, preserving previous cost evidence. Wiki file outcomes may now include `deferred` for infrastructure failures; applications should display that outcome without describing the original document as defective.

```python
from envai import EnvaiClient

with EnvaiClient(api_key, base_url=base_url) as client:
    uploaded = client.attachments.upload_file(workspace_id, chat_id, "invoice.pdf", idempotency_key="upload-invoice-1")
    ready = client.attachments.wait(workspace_id, chat_id, uploaded.attachment_id, timeout=600)
    if ready.lifecycle != "ready" or ready.conversion.status not in ("completed", "not_required"):
        raise RuntimeError("Resolve or remove the failed attachment before sending")
    result = client.chats.send_and_wait(chat_id, "Summarize the invoice.", attachment_ids=[ready.attachment_id], idempotency_key="message-invoice-1")
```

`AsyncEnvaiClient` provides the same attachment operations and `await client.attachments.wait(...)`. Async stream helpers retain `attachment_ids` and `idempotency_key`. `RequestOptions(cancel=threading.Event())` supports cooperative cancellation; async waits also honor task cancellation. Low-level methods support in-memory/seekable multipart input and never reopen/replay an ambiguous upload automatically. Download passes `kind="original"` or `kind="derived"` as a keyword argument.

See the matching backend [attachment guide](https://github.com/digitalygo/envai-teams/blob/7192fc05ebec817bddc705186c8f195cc0dc5ec5/docs/guides/chat-attachments.md) for limits, operator settings, and retention. Publishing this SDK does not deploy the backend.

With a stable message key, turn helpers retain the initial replay until the acknowledgment identifies the accepted turn. A previously completed turn can therefore finish from retained events without another inference call. The pre-send replay buffer is limited to 16 MiB; exceeding it fails before submitting the message. Resume a known turn with its event cursor when the retained stream is larger or a replay gap is reported.
