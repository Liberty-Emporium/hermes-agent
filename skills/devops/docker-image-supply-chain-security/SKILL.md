---
name: docker-image-supply-chain-security
description: "Secure Docker image supply chain with image signing, vulnerability scanning, and Content Trust. Covers Docker Content Trust (DCT), Cosign/Sigstore integration, SBOM generation, and multi-stage build hardening. Use when building production Docker images, securing CI/CD pipelines, or hardening container deployments."
version: "1.0"
author: Hermes Agent (Django Research)
---

# Docker Image Supply Chain Security

## Overview

Container image supply chain attacks are one of the top security risks in 2026. Attackers compromise base images, inject malicious layers, or tamper with images in registries. This skill covers the complete toolkit for securing your Docker image lifecycle from build to deployment.

**Key principle:** Never trust an image you didn't build, and always verify images before running them in production.

## When to Use

- Building production Docker images for customer deployments
- Setting up CI/CD pipelines that push to container registries
- Auditing existing Docker images for vulnerabilities
- Liberty Emporium: securing customer machine agent deployments

## Architecture

```
Source Code → Dockerfile → Build → Scan → Sign → Push → Verify → Deploy
     ↓            ↓          ↓       ↓       ↓       ↓        ↓        ↓
  Git commit   Multi-   BuildKit  Trivy  Cosign  Registry  Verify   Railway/
  verified     stage    cache     Grype  Notary  auth      before    Docker
```

## Step 1: Harden Your Dockerfile

### Use Multi-Stage Builds

```dockerfile
# Build stage
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Production stage - minimal attack surface
FROM python:3.12-slim AS production
# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser
WORKDIR /app
# Copy only installed packages, not build tools
COPY --from=builder /install /usr/local/
COPY --chown=appuser:appuser . .
# Remove shell access
RUN rm -rf /bin/sh /bin/bash /bin/dash
USER appuser
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Pin Base Image Digests

```dockerfile
# BAD: Floating tag can change
FROM python:3.12-slim

# GOOD: Pinned to specific digest (immutable)
FROM python:3.12-slim@sha256:abc123def456...

# Generate digest:
# docker pull python:3.12-slim
# docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
```

### Minimize Layers and Attack Surface

```dockerfile
# Combine RUN commands to reduce layers
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      curl \
      ca-certificates && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Don't install dev dependencies in production
RUN pip install --no-cache-dir -r requirements.txt
# NOT: pip install --no-cache-dir -r requirements-dev.txt
```

## Step 2: Enable Docker Content Trust (DCT)

Docker Content Trust ensures image integrity through cryptographic signing:

```bash
# Enable DCT for current session
export DOCKER_CONTENT_TRUST=1

# Or enable permanently
echo 'export DOCKER_CONTENT_TRUST=1' >> ~/.bashrc

# Now all pull/push operations verify signatures
docker pull libertyemporium/app:latest  # Verifies signature
docker push libertyemporium/app:v1.0.0  # Signs and pushes
```

### Set Up Notary for Your Registry

```bash
# Initialize trust for a new repository
docker trust key generate liberty-emporium

# Add a signer (for CI/CD)
docker trust signer add \
  --key liberty-emporium.pub \
  ci-signer \
  libertyemporium/app

# Sign an image
docker trust sign libertyemporium/app:v1.0.0

# Inspect signatures
docker trust inspect libertyemporium/app:v1.0.0 --pretty
```

## Step 3: Scan Images for Vulnerabilities

### Using Trivy (Recommended)

```bash
# Install Trivy
curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sh

# Scan a local image
trivy image libertyemporium/app:latest

# Scan with severity filter (fail CI on HIGH/CRITICAL)
trivy image --severity HIGH,CRITICAL --exit-code 1 libertyemporium/app:latest

# Scan a Dockerfile before building
trivy config Dockerfile

# Scan filesystem (without Docker)
trivy fs --severity HIGH,CRITICAL .
```

### Using Grype

```bash
# Install Grype
curl -sSfL https://raw.githubusercontent.com/anchore/grype/main/install.sh | sh

# Scan an image
grype libertyemporium/app:latest

# Output as JSON for CI processing
grype libertyemporium/app:latest -o json > scan-results.json

# Fail on severity threshold
grype --fail-on high libertyemporium/app:latest
```

## Step 4: Sign Images with Cosign (Sigstore)

Cosign is the modern alternative to Docker Content Trust, using the Sigstore ecosystem:

```bash
# Install Cosign
curl -sL https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64 -o cosign
chmod +x cosign
sudo mv cosign /usr/local/bin/

# Generate a key pair
cosign generate-key-pair

# Sign an image
cosign sign --key cosign.key libertyemporium/app:v1.0.0

# Verify a signature
cosign verify --key cosign.pub libertyemporium/app:v1.0.0

# Sign with keyless mode (uses OIDC identity)
cosign sign libertyemporium/app:v1.0.0
```

### Verify in CI/CD

```yaml
# .github/workflows/verify-image.yml
name: Verify Image Signature
on:
  pull_request:
    paths: ['Dockerfile']

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - name: Install Cosign
        uses: sigstore/cosign-installer@v3
      
      - name: Verify image signature
        run: |
          cosign verify \
            --key cosign.pub \
            libertyemporium/app:${{ github.sha }}
```

## Step 5: Generate SBOM (Software Bill of Materials)

An SBOM lists all components in your image — critical for supply chain transparency:

```bash
# Generate SBOM with Syft
syft libertyemporium/app:latest -o spdx-json > sbom.spdx.json

# Generate SBOM in CycloneDX format
syft libertyemporium/app:latest -o cyclonedx-json > sbom.cyclonedx.json

# Attach SBOM to image as attestation
cosign attest --predicate sbom.spdx.json \
  --key cosign.key \
  libertyemporium/app:v1.0.0

# Verify SBOM attestation
cosign verify-attestation --key cosign.pub \
  libertyemporium/app:v1.0.0
```

## Step 6: Implement in CI/CD Pipeline

```yaml
# .github/workflows/build-and-sign.yml
name: Build, Scan, Sign, Push

on:
  push:
    branches: [main]
    tags: ['v*']

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: liberty-emporium/app

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
      id-token: write  # For Cosign keyless

    steps:
      - uses: actions/checkout@v4

      - name: Build image
        run: docker build -t $REGISTRY/$IMAGE_NAME:${{ github.sha }} .

      - name: Scan with Trivy
        uses: aquasecurity/trivy-action@master
        with:
          image-ref: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:${{ github.sha }}
          severity: 'HIGH,CRITICAL'
          exit-code: '1'

      - name: Generate SBOM
        uses: anchore/sbom-action@v0
        with:
          image: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:${{ github.sha }}
          format: spdx-json
          output-file: sbom.spdx.json

      - name: Login to Registry
        uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Push image
        run: docker push $REGISTRY/$IMAGE_NAME:${{ github.sha }}

      - name: Install Cosign
        uses: sigstore/cosign-installer@v3

      - name: Sign image
        run: |
          cosign sign --yes \
            $REGISTRY/$IMAGE_NAME:${{ github.sha }}
```

## Pitfalls & Workarounds

* **DCT slows down builds:** Every pull/push requires signature verification.
  - **Workaround:** Use Cosign instead — it's faster and works with any registry.
  - **Workaround:** Disable DCT for local dev: `DOCKER_CONTENT_TRUST=0`

* **Base image digest pinning breaks when base updates:** You need to manually update digests.
  - **Workaround:** Use Renovate or Dependabot to auto-update digest pins.
  - **Workaround:** Use a digest that's updated by a trusted automation.

* **Trivy/Grype false positives:** Some CVEs don't affect your specific use case.
  - **Workaround:** Create an allowlist file (`.trivyignore`) for known false positives.
  - **Workaround:** Focus on fixable vulnerabilities: `trivy image --ignore-unfixed`

* **Cosign key management:** Losing the private key means you can't sign new images.
  - **Workaround:** Use keyless signing (OIDC-based) for CI/CD.
  - **Workaround:** Store keys in a KMS (AWS KMS, GCP KMS, HashiCorp Vault).

* **Registry doesn't support OCI attestations:** Some older registries don't support Cosign attestations.
  - **Workaround:** Use a modern registry (GHCR, ECR, GCR all support OCI artifacts).
  - **Workaround:** Store attestations in a separate Sigstore Rekor transparency log.

## Verification

```bash
# 1. Verify image signature
cosign verify --key cosign.pub libertyemporium/app:latest

# 2. Check for HIGH/CRITICAL vulnerabilities
trivy image --severity HIGH,CRITICAL --exit-code 1 libertyemporium/app:latest

# 3. Verify SBOM exists
cosign verify-attestation --key cosign.pub libertyemporium/app:latest

# 4. Check image is running as non-root
docker inspect libertyemporium/app:latest \
  --format '{{.Config.User}}'
# Should output "appuser" or a numeric UID, NOT empty (root)

# 5. Verify no secrets in image layers
docker history --no-trunc libertyemporium/app:latest | grep -i "secret\|password\|key\|token"
# Should return nothing
```

## Liberty Emporium Application

For customer machine agent deployments:

1. **Build** the Liberty Agent Docker image with multi-stage build
2. **Scan** for vulnerabilities before pushing
3. **Sign** with Cosign using CI/CD keyless signing
4. **Push** to private registry
5. **Verify** signature before deploying to customer machines
6. **Generate SBOM** for compliance documentation
