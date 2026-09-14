# Provisions ONE DigitalOcean droplet running the "lean production subset"
# of this app's docker-compose stack (see docker-compose.prod.yml): api +
# agent-workers + ingest-worker + postgres + redis + qdrant + litellm +
# ml-service, fronted by Caddy. Deliberately NOT the full dev stack
# (docker-compose.yml) — no local Ollama (litellm-config.prod.yaml points
# at a real hosted OpenAI-compatible endpoint instead), no Langfuse/MinIO/
# open-webui/observability containers. See infra/README.md for the full
# tradeoff and how to add any of those back.
terraform {
  required_version = ">= 1.6.0"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.42"
    }
  }

  # Local state by default — fine for the single-operator, run-deliberately
  # workflow this repo already uses for other impactful commands (`make
  # eval`/`garak-full`/`strix`, see .github/workflows/ci.yml's own header).
  # `terraform apply` here is likewise a human, manual action (see
  # infra/README.md) — CI never runs it, only `terraform plan` for
  # visibility on PRs touching infra/terraform/**.
  #
  # For a team (more than one operator applying) or if you ever want CI to
  # apply too, switch to a remote backend — DigitalOcean Spaces is
  # S3-compatible, so no extra provider is needed, just this block
  # uncommented and a Spaces bucket created first:
  #
  # backend "s3" {
  #   endpoints                   = { s3 = "https://nyc3.digitaloceanspaces.com" }
  #   bucket                      = "<your-spaces-bucket>"
  #   key                         = "agent-core-demo/terraform.tfstate"
  #   region                      = "us-east-1" # required by the S3 backend schema; ignored by Spaces
  #   skip_credentials_validation = true
  #   skip_metadata_api_check     = true
  #   skip_region_validation      = true
  #   skip_requesting_account_id  = true
  #   skip_s3_checksum            = true
  #   use_path_style              = true
  # }
  # Then: terraform init -migrate-state
}

provider "digitalocean" {
  token = var.do_token
}
