# MCP stable contract: live acceptance, 2026-10-13

Verified backend: `65ed3a2a254d06c4a7cb066d6cbd1ddcde899b5e` (PR #164), MCP SDK 2.3.0. Transport: existing Secure MCP Tunnel `tunnel_6ace6ae2cdd48191a59d80557486af4e`, service principal `ph_service_tunnel_projects_hub`. No personal user login is required; contextual actor/project do not replace service authentication or route authorization.

## Catalogue and subscription

The owner initially reported the legacy catalogue despite refreshing tools. Refresh traffic reached the updated backend. The owner subsequently refreshed again and allowed all tool risk levels; dot confirmed both events and all four tools, then created the stable subscription. These observations do not establish whether host caching or permissions caused the stale catalogue. No backend change or tunnel restart preceded recovery. Successful catalogue-response logging is added separately in PR #165.

Stable subscription: `sub_288a2273dde1bd289ce356046d274876bf82107a6faf8b8ada06d82ab148a409`, event `projects_hub.request.created`, sole filter `project_id=prj_9a50a8e9e45a495387cfd6d7413e833e`. Callback verification and active backend state were checked. The existing legacy subscription remains active.

## Fresh test: PASS

With dot waiting after its subscription confirmation, one request was created. No nonce was supplied to dot beforehand, and the operator did not send Slack messages or invoke response-writing tools.

| Fact | Value |
| --- | --- |
| Request | `mcp_test_780ede68e79d3037c4a3a05a526730c8` |
| Event | `evt_ede8f08b3c14c7b722cbb9b6ab16cdaa` |
| Response | `mcp_response_eb153911a8c69df6b3e2ac5085121273` |
| Created UTC | 18:25:45.800 |
| Callback accepted UTC | 18:25:47.053; first attempt, HTTP 200 |
| Saved UTC | 18:26:29.102 |
| Elapsed | 43.302 seconds |

Actual `get_project_request` and `record_request_result` calls were observed. The persisted typed `test_ack` contains the saved fresh nonce; the authenticated service is the response author. No live binding or Slack destination was created for this test. Callback acceptance alone was not used as the success criterion.

## Authorized discussion route: PASS

The owner explicitly authorized only replies in workspace `T0C82495TK5`, channel `C0C872SJFA7`, thread `1791913871.135149`. Its root was read and verified. Binding `mcp_binding_3d7213f4d5346ecfca68a126ff8f1ead`, revision 1, maps the real ready note `note_275b783b7bf022167bbebcfcb275b3f8` to that route and the same note for Mira reading. The setup/results thread was not used for test messages.

One live request used the same stable subscription and service grant as the test:

| Fact | Value |
| --- | --- |
| Request | `mcp_test_1ac5600d344f54fa6a5f598153f3bb54` |
| Event | `evt_2c299a7a423e638b738b6278fd7d1f5f` |
| Response | `mcp_response_2bafb2166c70b21b3029ed00155ca89f` |
| Existing-note reply | `reply_d9f1fca8770d3fa15e793a43fe80c755` |
| Created UTC | 18:27:01.097 |
| Backend saved UTC | 18:28:23.453; 82.356 seconds |
| Slack message | `1791916138.225049`, at 18:28:58.225 UTC |
| Slack receipt saved UTC | 18:29:21.670; 140.573 seconds |

Dot read the discussion, saved one `discussion_reply`, claimed Slack delivery, sent through its existing Slack connector and saved the actual receipt. Independent Slack thread readback found exactly one reply carrying the same response ID and matching message ID. The stored backend response was also read through the existing `CollaborationService.get_note/list_replies` path used by `project_note_get`.

Destination states are independently verified: **backend stored**, **Mira available**, **Slack confirmed**. Mira wake-up/delivery/acknowledgement is not claimed. This test verifies context exchange, not the product requirements mentioned in the discussion body.

## Scope and evidence

Only one live discussion/Slack route was authorized and tested. The original two-distinct-thread live acceptance remains unverified; two-route isolation, deduplication, restart persistence, revoked access, route revision conflicts and uncertain-send reconciliation passed local tests. Do not extend the live PASS to a second route.

Retained server evidence: `/home/dev/artifacts/projects-hub/20261013T175155Z-mcp-stable-contract-20261013/`, including `p0-stable-observation.json`, `mvp0-live-verification.json`, `slack-live-readback.json` and `mira-live-readback.json`. Nonces and credentials are excluded from these verification summaries. PR #164 passed 368 backend tests; the catalogue logging follow-up passed 60 targeted MCP/logging tests before CI.
