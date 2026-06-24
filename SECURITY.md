# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability in this project, please report it by emailing
aws-security@amazon.com. Do not report security vulnerabilities through public GitHub issues.


Known Security Considerations

The following items were identified during security review and accepted as appropriate trade-offs
for educational/demo code. Each is documented here for transparency.

| Item | Category | Rationale |
|------|----------|-----------|
| S3 uses SSE-S3 (AES256) instead of KMS CMK | Acceptable for Demo | AWS-managed encryption is sufficient for sample data; CMK adds cost and key management overhead |
| DynamoDB uses AWS-owned encryption key | Acceptable for Demo | Default encryption at rest is adequate for sample records |
| Lambda Pre-Token trigger not in VPC | Security Debt | Trigger only calls Cognito APIs (public endpoints); VPC adds 5-10s cold start latency to auth flow |
| S3 access logging not enabled | Security Debt | Recommended for production to audit object access; omitted for demo simplicity |
| Lambda missing Dead Letter Queue | Security Debt | Synchronous Cognito trigger — failed invocations return errors to the caller; DLQ not applicable |
| Broad exception handling in agent code | Acceptable for Demo | Prioritizes code readability for educational purposes |
| Error responses include debug details | Security Debt | Aids development and debugging; should be removed or sanitized in production |
| ECR `GetAuthorizationToken` uses `Resource: '*'` | Security Debt | AWS requires `Resource: '*'` for this action — cannot be scoped to a specific repository |

## Production Hardening Recommendations

Before using this code in a production environment, implement the following changes:

### Dependencies
- Pin all package versions in `requirements.txt` and `agent/requirements.txt` using exact versions (`==`) instead of open-ended ranges (`>=`)
- Use a lockfile (e.g., `pip-compile` or `pip freeze`) to capture the full dependency tree
- Regularly audit dependencies for known vulnerabilities using `pip-audit` or similar tools

### IAM & Access Control
- Scope `bedrock:InvokeModel` to the specific model ARN you use (e.g., `arn:aws:bedrock:us-east-1::foundation-model/us.anthropic.claude-sonnet-4-5-20250929-v1:0`)
- Scope `bedrock:Retrieve` to your specific Knowledge Base ARN once created
- Scope ECR image pull actions (`BatchGetImage`, `GetDownloadUrlForLayer`, `BatchCheckLayerAvailability`) to your specific repository ARN (e.g., `arn:aws:ecr:${Region}:${Account}:repository/customerhub-agent-*`)
- Keep `ecr:GetAuthorizationToken` with `Resource: '*'` in a separate IAM statement (AWS requires this)
- Consider adding `aws:RequestedRegion` condition to prevent cross-region access
- Review AgentCore Identity permissions (`bedrock-agentcore:*`) and scope to specific resource ARNs once stable

### Encryption
- Use KMS customer-managed keys (CMK) for Secrets Manager secrets (the `SalesforceTokenExchangeSecret` currently uses default AWS encryption)
- Use KMS CMK for DynamoDB table encryption (`SSESpecification` with `SSEType: KMS`)
- Use KMS CMK for S3 bucket encryption (switch from `AES256` to `aws:kms`)
- Enable KMS key rotation on all customer-managed keys
- Encrypt Lambda environment variables with a KMS key (`KmsKeyArn` property)
- Encrypt CloudWatch Logs groups with KMS if they contain sensitive query data

### Networking & Transport
- Enable S3 access logging to a dedicated logging bucket with lifecycle policies
- Consider VPC endpoints for Bedrock and DynamoDB if deploying the application in a VPC
- Enable VPC Flow Logs if using VPC-based deployment
- For Lambda triggers handling sensitive data in regulated environments, evaluate VPC deployment (note: adds cold start latency)

### Monitoring & Logging
- Enable CloudTrail data events for S3 and DynamoDB to audit data access
- Add CloudWatch alarms for failed authentication attempts (`InitiateAuth` failures) and authorization denials
- Add CloudWatch alarms for `AssumeRoleWithWebIdentity` failures (indicates potential token manipulation)
- Implement rate limiting on the Streamlit application layer
- Add anomaly detection for agent invocation patterns
- Monitor for unusual cross-department access attempts (even though they'll be denied by ABAC)

### Data Protection
- Enable DynamoDB deletion protection for production tables
- Configure S3 Object Lock for compliance-sensitive documents
- Implement data classification tagging and automated enforcement
- Add lifecycle policies for S3 objects and DynamoDB backups
- Enable DynamoDB Point-in-Time Recovery (already enabled in this demo)

### Application Security
- Add strict input validation and length limits for all user inputs to the agent
- Implement output filtering and content moderation for LLM responses
- Add request throttling per user/department
- Remove debug error details from production responses — return generic error messages and log details server-side only
- Use specific exception types instead of broad `except Exception`
- Add request/response logging (without sensitive data) for audit trails
- Implement session timeout and token refresh handling

### Container Security
- Pin base image to a specific digest (not just tag) for reproducible builds (e.g., `python:3.11-slim-bookworm@sha256:...`)
- Run vulnerability scanning on container images before deployment (e.g., ECR image scanning, Trivy)
- Implement image signing and verification
- Set resource limits (memory, CPU) on the container runtime
- Enable read-only root filesystem where possible

### Credential Rotation
- Implement automated rotation for the Salesforce client secret in Secrets Manager
- Monitor Cognito token lifetimes and enforce short-lived access tokens
- Rotate the OIDC thumbprint if the Cognito certificate chain changes (note: AWS ignores thumbprint for AWS-hosted OIDC endpoints, but update for correctness)

