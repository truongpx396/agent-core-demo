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
