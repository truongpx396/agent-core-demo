variable "do_token" {
  description = "DigitalOcean API token (create at https://cloud.digitalocean.com/account/api/tokens). Pass via TF_VAR_do_token or DIGITALOCEAN_TOKEN env var — never commit a real value."
  type        = string
  sensitive   = true
}

variable "project_name" {
  description = "Name for the DigitalOcean project grouping these resources, and prefix for resource names."
  type        = string
  default     = "agent-core-demo"
}

variable "environment" {
  description = "DigitalOcean project environment tag."
  type        = string
  default     = "Production"
  validation {
    condition     = contains(["Development", "Staging", "Production"], var.environment)
    error_message = "Must be one of: Development, Staging, Production (DigitalOcean project API's own allowed values)."
  }
}

variable "region" {
  description = "DigitalOcean region slug — see `doctl compute region list`."
  type        = string
  default     = "nyc3"
}

variable "droplet_size" {
  description = "Droplet size slug (`doctl compute size list`). The lean app profile (postgres + redis + qdrant + litellm + ml-service + api + agent-worker + ingest-worker, all on one box) needs real headroom — onnxruntime alone wants multiple cores free of contention (see docker-compose.yml's ml-service comment on measured CPU oversubscription under load). s-4vcpu-8gb is the practical floor; don't go below it."
  type        = string
  default     = "s-4vcpu-8gb"
}

variable "droplet_image" {
  description = "Base OS image slug."
  type        = string
  default     = "ubuntu-24-04-x64"
}

variable "ssh_key_fingerprints" {
  description = "Fingerprints of SSH keys already uploaded to your DO account (`doctl compute ssh-key list`) that get root access on first boot. Required — DigitalOcean emails a root password instead if this is empty, which this config doesn't support."
  type        = list(string)

  validation {
    condition     = length(var.ssh_key_fingerprints) > 0
    error_message = "At least one SSH key fingerprint is required."
  }
}

variable "admin_ip_cidrs" {
  description = "CIDR blocks allowed to reach SSH (22) and the app's direct debug port (8000). Find yours with `curl -s ifconfig.me`. Never leave this as 0.0.0.0/0."
  type        = list(string)

  validation {
    condition     = !contains(var.admin_ip_cidrs, "0.0.0.0/0") && !contains(var.admin_ip_cidrs, "::/0")
    error_message = "admin_ip_cidrs must not be world-open — SSH/debug access should be scoped to known operator IPs."
  }
}

variable "domain_name" {
  description = "Optional FQDN you point an A/AAAA record at the droplet's reserved IP for — informational only here (this module's own `app_url` output), the actual DOMAIN_NAME Caddy acts on lives in the droplet's .env (docker-compose.prod.yml), set independently of this variable. When set, Caddy requests a real Let's Encrypt cert for it; leave empty to serve plain HTTP over the reserved IP only (see Caddyfile)."
  type        = string
  default     = ""
}

variable "enable_backups" {
  description = "DigitalOcean's own weekly droplet-image backups (adds ~20% to droplet cost). Off by default: this app's actual data lives in Docker volumes (postgres/qdrant/redis), which a droplet-image backup does capture, but point-in-time DB dumps are a better fit for those than a whole-image snapshot — see infra/README.md's backup section for that alternative. Turn this on for a simpler, coarser safety net instead/as well."
  type        = bool
  default     = false
}

variable "deploy_user" {
  description = "Unprivileged Linux user created by cloud-init that owns /opt/agent-core-demo and runs `docker compose` there; the CI deploy workflow SSHes in as this user."
  type        = string
  default     = "deploy"
}

variable "deploy_ssh_public_key" {
  description = "Public key (contents of an id_ed25519.pub, e.g.) authorized for var.deploy_user over SSH — this is the identity .github/workflows/deploy.yml uses (its matching private key goes in the DEPLOY_SSH_KEY repo secret). Separate from ssh_key_fingerprints above, which grant root access for a human operator; keeping the CI identity as its own non-root user limits what a leaked deploy key could do."
  type        = string
}
