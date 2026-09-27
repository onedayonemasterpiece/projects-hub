# Projects Hub implementation rules

Owner vision and provenance live in docs/vision.md. This repository is the future replacement of Record Idea Hub, not permission to disable the working recorder/inbox.

Reuse live-interaction and the private deployment-installed ai-resource-control SDK. The only raw-key availability exception is the shared SDK's versioned authority-outage fallback, and Projects Hub is assigned only GOOGLE_API_KEY4. Do not implement fallback logic locally, borrow another consumer key, create a second audio/VAD implementation, or expose a service-role/provider key to the browser. Scope is authorized on the server. Content changes require expected revision and durable command id; assistant tool arguments cannot grant repository access.

Current code is a tested resource integration boundary, not a complete Android/backend product. Shared transport/resource authority is now versioned; this project still lacks a completed production runtime and real microphone/provider canary acceptance. Read docs/resource-rollout.md before calling a provider. Never describe a mock-only test as microphone/provider acceptance.
