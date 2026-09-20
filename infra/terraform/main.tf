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
    # No 8000 here — `api` no longer publishes a fixed host port (see
    # digitalocean_firewall.app's own comment on why).
    ufw_tcp_ports = [80, 443]
    app_dir       = "/opt/agent-core-demo"
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

# Separate, smaller droplet for Prometheus/Loki/Grafana/Alertmanager/
# otel-collector (docker-compose.observability.prod.yml) — deliberately not
# co-located with the app droplet so a metrics/logs spike can't compete with
# it for CPU/mem, and so losing this box never touches the app's own data.
# Same VPC as the app droplet (below) so the two can talk over DO's private
# network instead of the public internet — see digitalocean_firewall.observability's
# own comment for exactly which ports cross that boundary and why.
resource "digitalocean_droplet" "observability" {
  name       = "${var.project_name}-obs"
  region     = var.region
  size       = var.obs_droplet_size
  image      = var.droplet_image
  vpc_uuid   = digitalocean_vpc.this.id
  ssh_keys   = var.ssh_key_fingerprints
  monitoring = true
  backups    = false # metrics/logs only, bounded retention already (Loki 7d, Prometheus 15d) — not worth a whole-image backup

  user_data = templatefile("${path.module}/cloud-init.tpl.yaml", {
    deploy_user           = var.deploy_user
    deploy_ssh_public_key = var.deploy_ssh_public_key
    ufw_tcp_ports         = [80, 443, 3000, 4318, 3100]
    app_dir               = "/opt/agent-core-observability"
  })

  lifecycle {
    ignore_changes = [image]
  }
}

resource "digitalocean_reserved_ip" "observability" {
  region = var.region
}

resource "digitalocean_reserved_ip_assignment" "observability" {
  ip_address = digitalocean_reserved_ip.observability.ip_address
  droplet_id = digitalocean_droplet.observability.id
}

resource "digitalocean_project_resources" "this" {
  project = digitalocean_project.this.id
  resources = [
    digitalocean_droplet.app.urn,
    digitalocean_reserved_ip.app.urn,
    digitalocean_droplet.observability.urn,
    digitalocean_reserved_ip.observability.urn,
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

  # No port-8000 rule here anymore — `api` no longer publishes a fixed host
  # port (Caddyfile's `dynamic a` upstream needs `--scale api=N` to work at
  # all, which a static host-port mapping would block). The old
  # bypass-Caddy debug convenience this rule existed for is now `ssh` in +
  # `docker compose exec api curl localhost:8000/health` (infra/README.md).

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

# Observability droplet's firewall. Grafana is the only thing meant for a
# human browser, reached via Caddy on 80/443 — Prometheus/Loki/Alertmanager
# have no host-published port at all (Grafana reaches them over this
# droplet's own internal Docker network) and are never meant to be public.
# The only inbound traffic from the APP droplet is OTLP (4318, the
# otel-collector-agent forwarding metrics) and Loki's push endpoint (3100,
# Promtail shipping logs) — both scoped to this VPC's own CIDR, never
# 0.0.0.0/0, so they're unreachable from anywhere but the app droplet.
resource "digitalocean_firewall" "observability" {
  name        = "${var.project_name}-obs-fw"
  droplet_ids = [digitalocean_droplet.observability.id]

  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
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

  # Direct Grafana port, bypassing Caddy — operator-only debug convenience,
  # same CIDR restriction as SSH, never public.
  inbound_rule {
    protocol         = "tcp"
    port_range       = "3000"
    source_addresses = var.admin_ip_cidrs
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "4318"
    source_addresses = [digitalocean_vpc.this.ip_range]
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "3100"
    source_addresses = [digitalocean_vpc.this.ip_range]
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

  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}
