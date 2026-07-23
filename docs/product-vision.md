# Media Engine product vision

Media Engine is a product-neutral media processing platform. Products submit media to named, versioned pipelines and receive reusable artifacts without needing to know which worker or hardware backend performs the work.

The existing whY-Tee-WebDL integration is the first client. Future clients may include video-question-answering products, media libraries, podcast tooling, archive workflows, and automated preview or clip generation.

## Platform responsibilities

Media Engine owns:

- media ingestion, probing, and content identity;
- durable jobs, stages, leases, progress, cancellation, and retries;
- capability-based routing to CPU, Rockchip, NVIDIA, or future workers;
- transcoding, remuxing, audio extraction, subtitle extraction, scene detection, keyframes, OCR, and provider-neutral AI preparation;
- versioned artifacts, S3 storage, retention, and deletion;
- callbacks, audit metadata, health, and observability.

Client products own:

- source-specific acquisition such as YouTube and Instagram downloading;
- user interfaces, accounts, subscriptions, and product access rules;
- chat sessions, product prompts, and user-facing workflows.

## Stable contract

The platform contract is:

> Submit an asset to a server-defined pipeline and receive one or more versioned artifacts.

Clients select named pipelines instead of supplying arbitrary FFmpeg commands. The implemented family is `transcode`, `ai_prepare`, and `understand`; future versions can add normalization, embeddings, semantic search, and focused timeline retrieval without moving source acquisition or chat behavior into Media Engine.

The current `/jobs` API remains supported while the versioned platform API is introduced under `/v2`.
