# SyncBot on GCP (Terraform)

Cloud Run with **SQLite + Litestream → GCS** by default (free at low usage). Optional **existing MySQL / TiDB**. **Cloud SQL is not created.** Secrets are Terraform variables injected as Cloud Run env — no Secret Manager.

GitHub Actions never runs `terraform apply`. Keep `infra/gcp/terraform.tfstate` (local by default). A remote GCS backend is optional later.

## Free default vs paid warmth

`GCP_CLOUD_RUN_MIN_INSTANCES=0` is the **free default** (scale to zero; idle is not billed when `cpu_idle=true`). Combined with keep-warm (`GET /health` every 5 minutes, default on, free), the instance usually stays in Cloud Run’s idle window.

- On a cold start, Slack **events** (messages, reactions) are **queued by Cloud Run and/or retried by Slack**, so sync still happens — sometimes a few seconds later. Interactivity (buttons, modals, slash commands) may need a second click until the instance is warm.
- **Known rare instability:** the app currently mints a fresh post GUID per event and drops Slack retries to avoid duplicates. If the *first* delivery fails *before* sync work runs, the retry is not recovered and the event can be dropped. This is rare but real. A follow-up app change will make event handling idempotent; this caveat will be removed then.
- **If you need guaranteed no-drop responsiveness today** (for example production), set `GCP_CLOUD_RUN_MIN_INSTANCES=1` (paid always-on). That is the only default-adjacent knob that costs money; everything else is designed to stay in always-free quotas at low usage.
- Keep `cpu_idle=true` (request-based billing). If you turn CPU always-on, keep-warm pings become approximately as expensive as min_instances=1.
- Litestream streams WAL while CPU is allocated (during a request or keep-warm ping). A small RPO after the HTTP response is accepted for the free default. SIGTERM on the entrypoint flushes the replicator.

Operators who want Nation-style always-on: set min instances to 1.

## Upgrading from Cloud SQL

If you previously applied this module with Cloud SQL (`db-f1-micro`), `terraform apply` **destroys** that instance. Dump/backup first. There is no in-place migrate to SQLite in this tree — Litestream is a new database. Tulsa/sprocktech GCP was unused in production; forks that did apply Cloud SQL must backup before upgrading.

## Prerequisites

- [Terraform](https://www.terraform.io/downloads) >= 1.0
- [gcloud](https://cloud.google.com/sdk/docs/install) CLI, authenticated (`gcloud auth login` and Application Default Credentials)
- A GCP project with billing enabled
- Docker (for local image builds / CI)

## Operator checklist (GitHub deploy)

1. Copy `.env.deploy.example` → `.env.deploy.test`. Set `CLOUD_PROVIDER=gcp`, `GCP_PROJECT_ID`, Slack secrets. `GCP_DATABASE_MODE` defaults to `sqlite` (do **not** treat `DATABASE_HOST` as selecting TiDB). `GCP_CLOUD_RUN_MIN_INSTANCES` defaults to `0`; set `1` for guaranteed Slack 3s (paid).
2. `gcloud auth login` + ADC. Enable billing on the project.
3. `./deploy.sh --env test gcp` (or interactive `./deploy.sh gcp`): terraform apply. A placeholder image (`gcr.io/cloudrun/hello`) is allowed for the first apply. Confirm the plan **does not** create Cloud SQL. Confirm WIF output if `GITHUB_REPO` is set to **this** GitHub repo (`owner/repo` of the fork that has `test`/`prod`).
4. `infra/gcp/scripts/print-bootstrap-outputs.sh` → GitHub **repository or environment** vars: `GCP_PROJECT_ID`, `GCP_REGION`, `GCP_SERVICE_ACCOUNT`, `GCP_WORKLOAD_IDENTITY_PROVIDER`. Set `DEPLOY_TARGET=gcp`.
5. Slack manifest URLs from `service_url`. Install the app.
6. Confirm Deploy (AWS) jobs skip (`DEPLOY_TARGET=gcp`).
7. Push `test`. Deploy (GCP) builds `infra/gcp/Dockerfile` from the **repo root**, pushes, `gcloud run services update --image`. Later `terraform apply` must **not** revert the image (`lifecycle.ignore_changes`).
8. Repeat for `prod` with a separate state/stage (`-var=stage=prod`). This repo does not use Terraform workspaces.

Container: `docker build -f infra/gcp/Dockerfile --platform linux/amd64 .` from the repository root (not `infra/gcp/`).

## Database modes

| `database_mode` / `GCP_DATABASE_MODE` | Runtime | Notes |
| --- | --- | --- |
| `sqlite` (default) | `DATABASE_BACKEND=sqlite`, `DATABASE_URL=sqlite:////data/syncbot.db`, Litestream → GCS | `max_instances=1`, concurrency 1. No `DATABASE_PASSWORD`. |
| `existing` | MySQL / TiDB via host, user, password, schema | Same TiDB contract as AWS (port 4000, username prefix). No GCS bucket. Cloud Run may scale above 1 instance. |

`DATABASE_ENGINE=sqlite` is a synonym for sqlite mode. Do not infer mode from `DATABASE_HOST`.

## Variables (summary)

| Variable | Description |
|----------|-------------|
| `project_id` | GCP project ID (required) |
| `region` | Region (default `us-central1`) |
| `stage` | `test` or `prod` |
| `database_mode` | `sqlite` (default) or `existing` |
| `cloud_run_image` | Bootstrap default `gcr.io/cloudrun/hello`; CI replaces it |
| `cloud_run_min_instances` | `0` (default, free) or `1` (paid) |
| `enable_keep_warm` | Cloud Scheduler `/health` (default `true`) |
| `github_repo` | `owner/repo` for WIF; empty skips WIF |
| `slack_*` / `data_encryption_key` | App secrets (sensitive TF vars) |
| `existing_db_*` / `database_password` | Required when `database_mode=existing` |

See [variables.tf](variables.tf) and [example.tfvars](example.tfvars). Deploy file names: `GCP_CLOUD_RUN_IMAGE` (fallback `CLOUD_RUN_IMAGE`), `GCP_CLOUD_RUN_MIN_INSTANCES`, `ENABLE_KEEP_WARM` (unprefixed; portable name, AWS does not read it yet).

## Outputs

After `terraform apply`, [print-bootstrap-outputs.sh](scripts/print-bootstrap-outputs.sh) prints GitHub variable suggestions including `GCP_WORKLOAD_IDENTITY_PROVIDER`.

## HTTP port

Cloud Run sets `PORT` (typically `8080`). The container entrypoint listens on `PORT`.

## Security

The Cloud Run service is publicly invokable so Slack can reach it. Prefer WIF for GitHub Actions instead of long-lived keys. `github_repo` must match the deploying repository.
