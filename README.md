# Kubernetes State Controller — BootstrapKubernetes companion

A separate Kubernetes state controller with a responsive dashboard. Capture a healthy baseline, inject repeatable failures, then restore the pod images changed by the controller and watch readiness recover.

## Run

Python 3.9+; no Python dependencies. `python3 server.py` starts the **simulated demo** at http://127.0.0.1:4340. Demo state survives restart. Demo does not affect BootstrapKubernetes or its RCA results.

For real workloads, run on a machine with kubectl and authorized cluster credentials:

```sh
CONTROLLER_MODE=kubernetes KUBE_CONTEXT=your-test-context KUBE_NAMESPACES=app-one,app-two PORT=4340 python3 server.py
```

Explicit context and application namespaces are mandatory. `kube-*` namespaces are excluded. The adapter reads Deployments, ReplicaSets and Pods and patches pod container images. Only Deployment-managed pods are supported. It never switches the current kubectl context. RBAC needs list/get on those resources and get/patch on pods. Connect to the same cluster and namespaces BootstrapKubernetes observes; there is no assumed proprietary API integration.

## Flows

1. Capture baseline while all scoped Deployments are stable and ready.
2. Choose disaster (ceil 75% of pods **per Deployment**), partial outage (a chosen number in one Deployment), or a specific single pod.
3. Apply. Images become `rca-scenario.invalid/injected-failure:never`. This produces ErrImagePull / ImagePullBackOff, not a CPU, network or application exception simulation. Desired replica counts stay unchanged so unavailable replicas remain visible to RCA.
4. A five-second reconciliation loop maintains the failed-pod count, including replacement pods. Single-pod mode prefers the selected pod and follows a replacement in that Deployment if it disappears. Faults persist while the controller runs. Existing invalid images remain after it stops, but replacement pods are no longer reinjected.
5. Run the existing RCA analysis button in BootstrapKubernetes. The dashboard records the expected cause and scenario ID. There is no automatic RCA invocation, result collection or scoring until its API contract is supplied.
6. Restore normal state. Reconciliation stops reinjecting before rollback begins. Original container images are restored only on matching pod UIDs with unchanged injected images. Conflicts remain visible and retry every five seconds. Removed pods need no rollback. The UI says recovering until all current scoped Deployments are ready.

Reset reverses this controller's changes; it does not undo unrelated cluster edits, recreate removed workloads, or guarantee recovery from other faults. It does not change deployments, HPA, databases, volumes or replica counts. GitOps or other automation may fight pod changes; use an isolated test cluster. A process lock permits only one controller process per scope. Browser access grants control of the configured scope: listen on loopback, share only through a trusted Tailscale network. A same-origin session token protects mutation requests from cross-site requests; this is not multiuser authentication.

## Persistence and tracing

`.runtime/<scope>/state.json` contains durable baseline, active intent, a write-ahead rollback journal and the last 50 runs. Do not delete it during a scenario. `.runtime/<scope>/trace.jsonl` plus four rotated backups records structured timestamped events (1 MB each), app version, host, correlation IDs, actions, browser-visible state summaries, patch outcomes, failures and request durations. Images and identifiers used for rollback remain local; no secrets, raw app content or screenshots are logged. Logging errors do not break requests. Runtime files are excluded from Git. Avoid shared write access to this directory.

Read traces: `tail -n 60 .runtime/demo/trace.jsonl`. Find a scenario ID in the UI and search it with `rg '<scenario-id>' .runtime`. Browser actions carry a correlation ID through the backend. Background injections use the scenario ID. Observed readiness is distinct from API mutation success. An absent browser event does not prove the user saw a result.

Reproduce: capture → disaster → wait for faults-observed → restore → wait for normal. Error path: call apply without a baseline (using the session token from `/api/state`), or request more pods than exist. Run `python3 -m unittest discover -s tests -v` for restore, persistence, reconciliation and error coverage.

No AI is called by this app at runtime and no AI dependency registration is needed. Your existing BootstrapKubernetes RCA tool retains its own model configuration and credentials. Never put Kubernetes or provider credentials in browser code.

Automated integration check: `npm test` runs the controller tests and checks browser JavaScript syntax. Harness Push discovers this command through `package.json`. It requires Python 3.9+ and Node.js/npm, needs no installed packages or running server, and uses temporary simulated state without accessing Kubernetes.

Browser verification: `npm install --no-save --package-lock=false playwright`, then `node tests/browser.cjs` against the running demo. It exercises all three modes, reset, a visible validation failure, mutation CSRF protection and mobile layout. If Chromium is unavailable, run `npx playwright install chromium`.
