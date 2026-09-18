# Routing and device inventory

Nodes report safe facts: OS/architecture, CPU, detected GPU/VRAM, Hermes/Python versions, profile names, Kanban/browser/GUI availability, selected tools/skills/MCP names, terminal backends, local inference presence, load and free storage. They never report environment values, cookies, browser state or credentials.

Trust zone, allowed data classification, credential domains, maximum risk, concurrency, exclusive GPU and delegation depth are operator policy set through `PUT /api/v2/nodes/{id}/policy`; heartbeats cannot assert them.

Strict gates run before scoring: OS/architecture, tools/skills/MCP, browser/GUI/GPU/VRAM, repository and credential locality, classification/trust, risk, profile presence and capacity. Rejected alternatives return stable reason codes.

Accepted candidates score capability 25, availability 20, trust 20, locality 15, measured performance 10, historical success 5 and cost/energy 5, minus bounded load/network/risk penalties. Ties break by node ID for deterministic routing.

Defaults are global 8, node 2, profile 1, delegation depth 3, and exclusive GPU. Benchmarks are opt-in, install nothing, retain ten samples per `(node,name)`, and expose a 0.3 EWMA.

The inspected coordinator is detected as an NVIDIA RTX 3060 with approximately 12 GB VRAM. It should be described by detected hardware—not “Blackwell/Blackwave”—and initially used for coordination plus moderate local inference/engineering.
