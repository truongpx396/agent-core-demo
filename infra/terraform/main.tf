resource "digitalocean_project" "this" {
  name        = var.project_name
  description = "agent-core-demo lean production stack (see docker-compose.prod.yml)"
  purpose     = "Web Application"
  environment = var.environment
}

resource "digitalocean_vpc" "this" {
  name   = "${var.project_name}-vpc"
  region = var.region
}

# Stable IP that survives replacing the droplet (resize, image change,
# recreate after a bad apply) — DNS (domain_name) and the deploy workflow's
# DROPLET_HOST secret both point here, not at the droplet's own ephemeral
# ipv4_address, so neither needs updating across a droplet replacement.
resource "digitalocean_reserved_ip" "app" {
  region = var.region
}

resource "digitalocean_droplet" "app" {
  name       = "${var.project_name}-app"
  region     = var.region
  size       = var.droplet_size
  image      = var.droplet_image
  vpc_uuid   = digitalocean_vpc.this.id
  ssh_keys   = var.ssh_key_fingerprints
  monitoring = true
  backups    = var.enable_backups

  user_data = templatefile("${path.module}/cloud-init.tpl.yaml", {
    deploy_user           = var.deploy_user
    deploy_ssh_public_key = var.deploy_ssh_public_key
  })

  # cloud-init (user_data) only runs on FIRST boot — editing it later and
  # re-applying wouldn't actually re-provision the running droplet, just
  # silently drift Terraform's view of it from reality. tainting/replacing
  # is the correct way to pick up a cloud-init change; this makes that
  # deliberate rather than accidentally masked.
  lifecycle {
    ignore_changes = [image] # DO backups can rotate the base image id out from under this without this being a real drift
  }
}

resource "digitalocean_reserved_ip_assignment" "app" {
  ip_address = digitalocean_reserved_ip.app.ip_address
  droplet_id = digitalocean_droplet.app.id
}

resource "digitalocean_project_resources" "this" {
  project = digitalocean_project.this.id
  resources = [
    digitalocean_droplet.app.urn,
    digitalocean_reserved_ip.app.urn,
  ]
}

# Cloud firewall — the FIRST enforcement layer (cloud-init's ufw rules on
# the droplet itself are the second, in case it's ever moved out of
# droplet_ids). Default-deny: only what's explicitly listed below is
# reachable; everything else inbound is dropped.
resource "digitalocean_firewall" "app" {
  name        = "${var.project_name}-fw"
  droplet_ids = [digitalocean_droplet.app.id]

  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = var.admin_ip_cidrs
  }

  # Direct app port, bypassing Caddy — for `curl`-ing /health or debugging
  # a bad deploy without going through the reverse proxy. Operator-only,
  # same CIDR restriction as SSH, never public.
  inbound_rule {
    protocol         = "tcp"
    port_range       = "8000"
    source_addresses = var.admin_ip_cidrs
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "80"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "443"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  # ICMP outbound — path MTU discovery / operator pings out from the box.
  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}
