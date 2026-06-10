---
name: github-actions-oidc-security
description: "Secure GitHub Actions deployments using OpenID Connect (OIDC) to eliminate long-lived cloud secrets. Covers AWS, Azure, GCP, and HashiCorp Vault OIDC integration patterns. Use when setting up CI/CD pipelines that deploy to cloud providers or need to access secrets without storing credentials."
version: "1.0"
author: Hermes Agent (Django Research)
---

# GitHub Actions OIDC Security

## Overview

OpenID Connect (OIDC) allows GitHub Actions workflows to authenticate with cloud providers **without storing any long-lived secrets**. Instead of AWS access keys or Azure service principals in GitHub secrets, you configure a trust relationship between GitHub's OIDC token issuer and your cloud provider.

**Key benefit:** No secrets to rotate, no credentials to leak. The OIDC token is short-lived (minutes) and scoped to a specific workflow run.

## When to Use

- Deploying from GitHub Actions to AWS, Azure, GCP, or Railway
- Accessing cloud secrets from CI/CD without storing credentials
- Any workflow that currently uses `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in GitHub secrets
- Liberty Emporium: securing deployment pipelines for customer apps

## How OIDC Works

```
GitHub Actions Workflow
    ↓ requests OIDC token
GitHub OIDC Provider (https://token.actions.githubusercontent.com)
    ↓ issues JWT token (short-lived, scoped)
Cloud Provider (AWS/Azure/GCP)
    ↓ validates token claims
    ↓ checks: repository, branch, workflow, environment
Temporary credentials issued
    ↓ used for deployment
Target resource
```

The JWT token contains claims like:
- `sub` (subject): `repo:owner/repo:ref:refs/heads/main`
- `repository`: `owner/repo`
- `workflow`: workflow name
- `environment`: production/staging
- `actor`: GitHub username who triggered the run

## Step 1: Configure AWS OIDC (Most Common)

### 1a: Create OIDC Identity Provider in AWS IAM

```bash
# Create the OIDC provider for GitHub
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 \
  --client-id-list sts.amazonaws.com
```

### 1b: Create IAM Role with Trust Policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:Liberty-Emporium/*:ref:refs/heads/main"
        }
      }
    }
  ]
}
```

### 1c: Use in GitHub Actions Workflow

```yaml
# .github/workflows/deploy.yml
name: Deploy to AWS

permissions:
  id-token: write   # REQUIRED for OIDC
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Configure AWS Credentials via OIDC
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::ACCOUNT_ID:role/GitHubActionsDeployRole
          aws-region: us-east-1
      
      - name: Deploy
        run: |
          aws s3 sync ./dist s3://my-bucket/
          aws cloudfront create-invalidation --distribution-id $DIST_ID --paths "/*"
```

## Step 2: Configure Azure OIDC

```yaml
# .github/workflows/deploy-azure.yml
name: Deploy to Azure

permissions:
  id-token: write
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Azure Login via OIDC
        uses: azure/login@v2
        with:
          client-id: ${{ secrets.AZURE_CLIENT_ID }}
          tenant-id: ${{ secrets.AZURE_TENANT_ID }}
          subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
      
      - name: Deploy to Azure App Service
        uses: azure/webapps-deploy@v3
        with:
          app-name: 'liberty-app'
          slot-name: 'production'
          package: ./dist
```

**Note:** Azure still needs the App Registration's client/tenant/subscription IDs, but these are not secrets — they're public identifiers. The actual authentication uses the OIDC token.

## Step 3: Configure GCP OIDC

```yaml
# .github/workflows/deploy-gcp.yml
name: Deploy to GCP

permissions:
  id-token: write
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Authenticate to GCP
        uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/providers/github-provider
          service_account: github-actions@PROJECT_ID.iam.gserviceaccount.com
      
      - name: Deploy to Cloud Run
        uses: google-github-actions/deploy-cloudrun@v2
        with:
          service: liberty-app
          region: us-central1
          source: .
```

## Step 4: Configure HashiCorp Vault OIDC

For accessing secrets from Vault without storing Vault tokens:

```yaml
# .github/workflows/deploy-with-secrets.yml
name: Deploy with Vault Secrets

permissions:
  id-token: write
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Import Secrets from Vault
        uses: hashicorp/vault-action@v3
        with:
          method: jwt
          url: https://vault.alexanderai.site
          role: github-actions-role
          secrets: |
            secret/data/deploy/stripe_key STRIPE_KEY |
            secret/data/deploy/db_url DATABASE_URL
      
      - name: Deploy with secrets
        run: ./deploy.sh
        env:
          STRIPE_KEY: ${{ steps.vault.outputs.STRIPE_KEY }}
          DATABASE_URL: ${{ steps.vault.outputs.DATABASE_URL }}
```

## Step 5: Secure Your OIDC Configuration

### Restrict by Environment

```json
{
  "Condition": {
    "StringEquals": {
      "token.actions.githubusercontent.com:sub": "repo:Liberty-Emporium/app:environment:production"
    }
  }
}
```

### Restrict by Branch

```json
{
  "Condition": {
    "StringLike": {
      "token.actions.githubusercontent.com:sub": "repo:Liberty-Emporium/app:ref:refs/heads/main"
    }
  }
}
```

### Restrict by Tag (for releases)

```json
{
  "Condition": {
    "StringLike": {
      "token.actions.githubusercontent.com:sub": "repo:Liberty-Emporium/app:ref:refs/tags/v*"
    }
  }
}
```

## Step 6: Add Workflow Protection Rules

Prevent workflow tampering:

```yaml
# Require approval for production deployments
environment:
  name: production
  url: https://app.alexanderai.site

# In GitHub repo settings:
# - Settings → Environments → production → Protection rules
# - Required reviewers: 1 (Jay)
# - Wait timer: 5 minutes (cool-down period)
# - Deployment branches: main only
```

## Pitfalls & Workarounds

* **OIDC token not available in fork PRs:** By default, `id-token: write` is not available for pull requests from forks.
  - **Fix:** Use `pull_request_target` instead of `pull_request` for the trigger (but be careful with code execution).
  - **Fix:** Require fork PRs to be reviewed before OIDC-enabled jobs run.

* **Overly permissive trust policy:** Using `repo:*` allows any repo to assume the role.
  - **Fix:** Always scope to specific repos: `repo:Liberty-Emporium/specific-repo`
  - **Fix:** Add environment and branch conditions.

* **Thumbprint changes:** GitHub's OIDC thumbprint can change.
  - **Fix:** Use multiple thumbprints in the provider config.
  - **Fix:** Monitor for authentication failures after GitHub updates.

* **Role ARN in workflow is not a secret:** The role ARN is a public identifier, not a credential.
  - **Note:** It's safe to hardcode the role ARN in the workflow file. The security comes from the trust policy conditions.

* **Reusable workflows need special handling:** The OIDC token subject includes the calling workflow, not the reusable workflow.
  - **Fix:** Use `job_workflow_ref` claim to validate the calling workflow path.

## Verification

Test your OIDC setup:

```bash
# 1. Verify OIDC provider exists
aws iam get-open-id-connect-provider \
  --open-id-connect-provider-arn arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com

# 2. Verify trust policy
aws iam get-role --role-name GitHubActionsDeployRole \
  --query 'Role.AssumeRolePolicyDocument'

# 3. Test with a workflow run
# Push to main and check the Actions tab for successful authentication

# 4. Verify no long-lived secrets in repo
gh secret list
# Should NOT show AWS_ACCESS_KEY_ID or AWS_SECRET_ACCESS_KEY
```

## Migration Checklist

When migrating from secrets-based to OIDC-based auth:

- [ ] Create OIDC identity provider in cloud console
- [ ] Create IAM role with appropriate trust policy
- [ ] Add `permissions: id-token: write` to workflow
- [ ] Replace secret-based auth step with OIDC auth step
- [ ] Test deployment succeeds
- [ ] Remove old cloud secrets from GitHub repository
- [ ] Update branch protection rules
- [ ] Add environment protection rules for production
- [ ] Document the new auth flow for team members
