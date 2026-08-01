# Orchestration Templates

These templates are executable task graphs for the next scheduler phase. Every mutable or external action carries an approval policy.

```yaml
research_syndicate:
  steps:
    - {id: decompose, profile: orchestrator, action: decompose_topic}
    - {id: research, profiles: [research-1, research-2, research-3], action: independent_research, needs: [decompose]}
    - {id: evidence, profile: evidence-reviewer, action: verify_claims, needs: [research]}
    - {id: synthesis, profile: synthesizer, action: produce_report, needs: [evidence]}
    - {id: judge, profile: judge, action: check_acceptance, needs: [synthesis]}
software_build_team:
  steps:
    - {id: architecture, profile: architect}
    - {id: implementation, profile: implementation-agent, needs: [architecture]}
    - {id: tests, profile: test-agent, needs: [implementation]}
    - {id: security, profile: security-reviewer, needs: [implementation]}
    - {id: ui, profile: ui-reviewer, needs: [implementation]}
    - {id: integration, profile: final-integration-agent, needs: [tests, security, ui]}
device_diagnostics:
  approval: required_before_changes
  steps: [collect_node_metrics, inspect_hermes_health, inspect_service_logs, identify_likely_fault, propose_repair]
local_gpu_workload:
  bounded: true
  steps: [verify_cuda_vram, select_local_model, run_bounded_job, monitor_resources, save_output, release_resources]
bci_development:
  steps:
    - {profile: bci-researcher}
    - {profile: python-lsl-engineer}
    - {profile: node-red-integration-agent}
    - {profile: visualisation-designer}
    - {profile: validation-agent}
```
