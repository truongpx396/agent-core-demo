output "droplet_id" {
  value = digitalocean_droplet.app.id
}

output "droplet_ipv4" {
  description = "The droplet's own (non-reserved) public IP — informational only; use reserved_ip below for anything durable (DNS, CI secrets)."
  value       = digitalocean_droplet.app.ipv4_address
}

output "reserved_ip" {
  description = "Stable IP that survives a droplet replacement. Point domain_name's A record here, and set this as the DROPLET_HOST GitHub Actions secret (see infra/README.md)."
  value       = digitalocean_reserved_ip.app.ip_address
}

output "ssh_command" {
  value = "ssh ${var.deploy_user}@${digitalocean_reserved_ip.app.ip_address}"
}

output "app_url" {
  value = var.domain_name != "" ? "https://${var.domain_name}" : "http://${digitalocean_reserved_ip.app.ip_address}"
}

output "observability_reserved_ip" {
  description = "Stable public IP for the observability droplet — point obs_domain_name's A record here, and set this as the OBS_DROPLET_HOST GitHub Actions secret."
  value       = digitalocean_reserved_ip.observability.ip_address
}

output "observability_private_ipv4" {
  description = "The observability droplet's private VPC IP — paste this into the APP droplet's .env as OBS_COLLECTOR_ENDPOINT (http://<this>:4318) and LOKI_PUSH_HOST (<this>:3100) before its otel-collector-agent/promtail can reach it. Never used over the public internet."
  value       = digitalocean_droplet.observability.ipv4_address_private
}

output "observability_ssh_command" {
  value = "ssh ${var.deploy_user}@${digitalocean_reserved_ip.observability.ip_address}"
}

output "observability_url" {
  value = var.obs_domain_name != "" ? "https://${var.obs_domain_name}" : "http://${digitalocean_reserved_ip.observability.ip_address}"
}
