# Example Terraform variables for infra/gcp (do not commit real secrets).
# Copy to terraform.tfvars locally if you apply by hand instead of ./deploy.sh.

project_id = "your-gcp-project-id"
region     = "us-central1"
stage      = "test"

# sqlite (default, Litestream + GCS) or existing (TiDB / other MySQL)
database_mode = "sqlite"

# Bootstrap image; CI replaces it. Terraform ignores subsequent image changes.
cloud_run_image = "gcr.io/cloudrun/hello"

# 0 = free/best-effort (default). 1 = paid always-on for Slack 3s.
cloud_run_min_instances = 0
enable_keep_warm        = true

# owner/repo of the GitHub repo that will push to test/prod (your fork, not necessarily sprocktech/syncbot)
github_repo = ""

slack_signing_secret = "replace-me"
slack_client_id      = "111.222"
slack_client_secret  = "replace-me"
data_encryption_key  = "replace-with-token-urlsafe-36"

# Required only when database_mode = existing
# existing_db_host     = "gateway.tidbcloud.com"
# database_password    = "replace-me"
# database_port        = "4000"
# database_backend     = "mysql"
# existing_db_username_prefix = "abc123"
