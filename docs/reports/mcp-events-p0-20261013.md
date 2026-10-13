# Projects Hub MCP Events P0: live result

Legacy-event P0 PASS on 2026-10-13 through the owner's actual ChatGPT dot and
Secure MCP Tunnel. Times below are UTC (Kaliningrad: UTC+2).

- Deployed backend: `256a8cc83df8fd013673facfb977b86981a02837`; MCP SDK 2.3.0.
- Endpoint: `https://projects-hub.kenigevents.ru/mcp`; tunnel
  `tunnel_6ace6ae2cdd48191a59d80557486af4e`.
- Project: `prj_9a50a8e9e45a495387cfd6d7413e833e` (Projects Hub).
- Subscription: `sub_12e3adda9d3d93330a3428186f17520f53371452a727e7302665308d2cef9777`.
- Request: `mcp_test_ce3e9aed90d0a4dc70787529421a47a2`.
- Event: `evt_cf4595190d326748dadc0871e63a6a33`, name
  `projects_hub.test_requested`; one fresh request, one delivery attempt.
- Created: 17:46:47.413; callback accepted HTTP 200: 17:46:48.365.
- Live `get_test_request`: 17:47:17.103; durable `record_test_response`:
  17:47:24.554, elapsed 37.141 seconds.
- Authenticated response author:
  `mcp_service_61fbf18d3c6d83336d5cbd0536ff3efd` (dedicated tunnel integration).

The owner confirmed the dot had subscribed and was ready. No new message to the
dot was reported during observation. The nonce was created afterward, omitted
from setup/event payload and read through the tool. Registered live logs show
read then write; independent read-only SQLite verification confirms completed
state, a saved response containing the fresh nonce, the integration author and
matching project/context/request/event. No synthetic tool response or Slack wake
was used. Callback acceptance alone was not treated as PASS.

Retained exact status/response and verification:
`/home/dev/artifacts/projects-hub/20261013T173034Z-mcp-secure-tunnel-20261013/p0-live-status.json`
and `p0-live-observation.json`. Credentials are absent from these reports.

This establishes the original P0 vertical route. The subsequently added stable
contract requires its own managed subscription transition and live test; its
acceptance and MVP0 discussion/Slack delivery are not inferred from this result.
The public OAuth form failure remains unexplained; verified tunnel creation and
live delivery provide the operational workaround.
