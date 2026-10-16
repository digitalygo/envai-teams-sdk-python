# Chat attachments

This unreleased feature requires a matching backend advertising `chat_attachments` in contract-3 capabilities. The SDK checks this before attachment mutations and messages containing attachment IDs. It never falls back to sending the text alone.

Upload one immutable original with an upload idempotency key, poll its conversion, then send ordered attachment IDs with a separate stable message idempotency key. A message acknowledgment must contain those same IDs. Pending or failed selected files block the whole turn; explicitly retry a permitted conversion or remove that file before sending. Successful conversions are reused. Poll timeouts cancel only the local wait. Mutations are never automatically retried, even with a key: reconcile an uncertain response before explicitly replaying the same payload/key.

Supported inputs are `.md`, `.txt`, `.csv`, `.xlsx`, `.pdf`, `.docx`, `.mp3`, and `.mp4`. PDF/DOCX use existing Mistral OCR; MP3 and the mixed audio tracks of MP4 use direct ElevenLabs Scribe. MP4 visual frames are not analyzed. Native workbook conversion does not execute formulas. There is no model-specific multimodal routing, automatic Wiki indexing, guaranteed Office editing/export, or automatic mounting of original files in tools.

The complete textual projection must fit the server and model context budgets; oversized input fails explicitly and the converted attachment stays available. No silent truncation occurs. Originals and derivatives remain private and scoped to the owning user, workspace and chat. Forked/restored text does not grant access to originals. Status/list/download/retry/delete are separate operations. Download selects `kind=original` or `kind=derived`.

Draft retention defaults to 24 hours; accepted attachments and input snapshots default to 14 days. Idempotent replay is guaranteed while the retained input snapshot exists. Withdrawal blocks new reads immediately; physical cleanup may wait for live conversion leases. Deletion does not erase already accepted conversation content.

Conversion cost is exposed in the attachment's `usage_cost`; inference is attributed to the turn. Missing tariffs and uncertain outcomes stay pending with null totals. An explicit conversion retry creates a linked accounting cycle, preserving previous cost evidence. Wiki file outcomes may now include `deferred` for infrastructure failures; applications should display that outcome without describing the original document as defective.

```python
from envai import EnvaiClient

with EnvaiClient(api_key, base_url=base_url) as client:
    uploaded = client.attachments.upload_file(workspace_id, chat_id, "invoice.pdf", idempotency_key="upload-invoice-1")
    ready = client.attachments.wait(workspace_id, chat_id, uploaded.attachment_id, timeout=600)
    if ready.lifecycle != "ready" or ready.conversion.status != "completed":
        raise RuntimeError("Resolve or remove the failed attachment before sending")
    result = client.chats.send_and_wait(chat_id, "Summarize the invoice.", attachment_ids=[ready.attachment_id], idempotency_key="message-invoice-1")
```

`AsyncEnvaiClient` provides the same attachment operations and `await client.attachments.wait(...)`. Async stream helpers retain `attachment_ids` and `idempotency_key`. `RequestOptions(cancel=threading.Event())` supports cooperative cancellation; async waits also honor task cancellation. Low-level methods support in-memory/seekable multipart input and never reopen/replay an ambiguous upload automatically. Download passes `kind="original"` or `kind="derived"` as a keyword argument.

See the matching backend [attachment guide](https://github.com/digitalygo/envai-teams/blob/96acf00162984af4a86a671f924c53a18960fae6/docs/guides/chat-attachments.md) for limits, operator settings, and retention. Publishing this SDK does not deploy the backend.

With a stable message key, turn helpers retain the initial replay until the acknowledgment identifies the accepted turn. A previously completed turn can therefore finish from retained events without another inference call. The pre-send replay buffer is limited to 16 MiB; exceeding it fails before submitting the message. Resume a known turn with its event cursor when the retained stream is larger or a replay gap is reported.
