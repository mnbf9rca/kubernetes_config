# 1Password-backed env var template for the homelab cluster.
# Used by `op run --env-file=.env.tpl -- <command>` to inject resolved
# secret values into a process environment without ever touching disk.
#
# Launch Claude with:
#   op run --env-file=.env.tpl -- claude
#
# Or run any make target ad-hoc:
#   op run --env-file=.env.tpl -- make apply-homelab
#
# Per-service secrets are commented out and should be uncommented as
# each workload is migrated (Phase 4).

# --- Platform secrets (needed from Phase 2 onward) ---

# Restic / Backblaze B2 (Phase 3 backup system)
B2_ACCOUNT_ID=op://Homelab/b2-restic/account-id
B2_ACCOUNT_KEY=op://Homelab/b2-restic/account-key
RESTIC_PASSWORD=op://Homelab/b2-restic/repo-password
RESTIC_REPOSITORY=op://Homelab/b2-restic/repository

# Route53 credentials for cert-manager DNS-01 (Task 2.5)
ROUTE53_ACCESS_KEY_ID=op://Homelab/route53-cert-manager/access-key-id
ROUTE53_SECRET_ACCESS_KEY=op://Homelab/route53-cert-manager/secret-access-key

# ACME contact email for Let's Encrypt (Task 2.5)
ACME_EMAIL=op://Homelab/acme/email

# Tailscale auth key for the siderolabs/tailscale extension (Task 2.8)
#TAILSCALE_AUTH_KEY=op://Homelab/tailscale/homelab-auth-key

# Jottacloud backup creds (Phase 4, jottacloud-backup workload)
#JOTTA_USERNAME=op://Homelab/jottacloud/username
#JOTTA_PASSWORD=op://Homelab/jottacloud/password
