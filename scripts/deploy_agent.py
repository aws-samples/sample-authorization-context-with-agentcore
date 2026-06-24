"""
Deploy per-department CustomerHub CRM Agents to AgentCore Runtime.

Deploys the same container image twice — one per department (Sales,
Finance). Each deployment gets:
  - Its own department-specific IAM execution role (from CloudFormation)
  - A customClaims authorizer that rejects tokens where the department
    claim doesn't match the target department

Usage:
    python scripts/deploy_agent.py --env dev
    python scripts/deploy_agent.py --env dev --skip-build
    python scripts/deploy_agent.py --env dev --department Sales

Requires:
    - Docker with buildx support (or Finch)
    - AWS CLI configured
    - Environment variables in .env file
    - CloudFormation stack deployed (for IAM roles)
"""

import argparse
import json
import os
import subprocess
import sys
import time

import boto3
from dotenv import load_dotenv

load_dotenv()

AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')
COGNITO_USER_POOL_ID = os.getenv('COGNITO_USER_POOL_ID', '')
COGNITO_APP_CLIENT_ID = os.getenv('COGNITO_APP_CLIENT_ID', '')

# Salesforce configuration (Pattern 2 - External Services via AgentCore Identity Token Exchange)
SALESFORCE_INSTANCE_URL = os.getenv('SALESFORCE_INSTANCE_URL', '')
SALESFORCE_PROVIDER_NAME = os.getenv('SALESFORCE_PROVIDER_NAME', '')

DEPARTMENTS = ['Sales', 'Finance']


def get_account_id() -> str:
    sts = boto3.client('sts', region_name=AWS_REGION)
    return sts.get_caller_identity()['Account']


def get_stack_outputs(env: str) -> dict:
    """Get outputs from the CloudFormation stack."""
    cfn = boto3.client('cloudformation', region_name=AWS_REGION)
    stack_name = "crmhub"
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
        outputs = resp['Stacks'][0]['Outputs']
        return {o['OutputKey']: o['OutputValue'] for o in outputs}
    except Exception as e:
        print(f"Error reading stack '{stack_name}': {e}")
        print("Make sure the CloudFormation stack is deployed first.")
        sys.exit(1)


def create_ecr_repository(repo_name: str) -> str:
    ecr = boto3.client('ecr', region_name=AWS_REGION)
    account_id = get_account_id()
    try:
        ecr.describe_repositories(repositoryNames=[repo_name])
        print(f"ECR repository '{repo_name}' already exists.")
    except ecr.exceptions.RepositoryNotFoundException:
        print(f"Creating ECR repository '{repo_name}'...")
        ecr.create_repository(
            repositoryName=repo_name,
            imageScanningConfiguration={'scanOnPush': True}
        )
    return f"{account_id}.dkr.ecr.{AWS_REGION}.amazonaws.com/{repo_name}"


def build_and_push_image(repo_uri: str, tag: str = 'latest') -> str:
    """Build container image and push to ECR."""
    account_id = get_account_id()

    # Detect container runtime
    finch_check = subprocess.run(["which", "finch"], capture_output=True)
    runtime = "finch" if finch_check.returncode == 0 else "docker"
    print(f"Using container runtime: {runtime}")

    # Login to ECR (avoid shell pipe: capture password, then pipe via stdin)
    print("Logging in to ECR...")
    ecr_endpoint = f"{account_id}.dkr.ecr.{AWS_REGION}.amazonaws.com"
    password = subprocess.run(
        ["aws", "ecr", "get-login-password", "--region", AWS_REGION],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subprocess.run(
        [runtime, "login", "--username", "AWS", "--password-stdin", ecr_endpoint],
        check=True,
        input=password,
        text=True,
    )

    image_uri = f"{repo_uri}:{tag}"
    print(f"Building image: {image_uri}")
    subprocess.run([runtime, "build", "-t", image_uri, "agent/"], check=True)

    print(f"Pushing image to {image_uri}...")
    subprocess.run([runtime, "push", image_uri], check=True)

    print(f"Image pushed: {image_uri}")
    return image_uri


def get_cognito_discovery_url() -> str:
    if not COGNITO_USER_POOL_ID:
        raise ValueError("COGNITO_USER_POOL_ID not set in .env")
    return (
        f"https://cognito-idp.{AWS_REGION}.amazonaws.com"
        f"/{COGNITO_USER_POOL_ID}/.well-known/openid-configuration"
    )


def deploy_department_agent(
    department: str,
    image_uri: str,
    role_arn: str,
    env: str,
    table_name: str,
    bucket_name: str,
    kb_id: str,
    user_scoped_role_arn: str = "",
) -> dict:
    """Deploy a single department agent with customClaims authorizer."""
    client = boto3.client('bedrock-agentcore-control', region_name=AWS_REGION)

    agent_name = f"customerhub_{department.lower()}_{env}"
    discovery_url = get_cognito_discovery_url()
    client_id = COGNITO_APP_CLIENT_ID

    if not client_id:
        raise ValueError("COGNITO_APP_CLIENT_ID not set in .env")

    # customClaims authorizer: AgentCore rejects tokens where the
    # department claim doesn't equal this department value.
    # A Sales user's token hitting the Finance agent → 401.
    #
    # We use allowedClients (validates the JWT 'client_id' claim) rather
    # than allowedAudience (validates 'aud') because Cognito access tokens
    # carry the app client identifier in 'client_id', not 'aud'. The V2
    # Pre-Token Generation trigger injects the 'department' claim into
    # the access token, which is what AgentCore validates here.
    authorizer_config = {
        "customJWTAuthorizer": {
            "discoveryUrl": discovery_url,
            "allowedClients": [client_id],
            "customClaims": [
                {
                    "inboundTokenClaimName": "department",
                    "inboundTokenClaimValueType": "STRING",
                    "authorizingClaimMatchValue": {
                        "claimMatchValue": {
                            "matchValueString": department
                        },
                        "claimMatchOperator": "EQUALS"
                    }
                }
            ]
        }
    }

    env_vars = {
        'DYNAMODB_TABLE': table_name,
        'S3_BUCKET': bucket_name,
        'KNOWLEDGE_BASE_ID': kb_id,
        'AWS_REGION': AWS_REGION,
        'AWS_DEFAULT_REGION': AWS_REGION,
        # Bedrock model used by the Strands agent. Override here so we can
        # rotate models without rebuilding the container image.
        'MODEL_ID': os.getenv('MODEL_ID',
                              'us.anthropic.claude-sonnet-4-5-20250929-v1:0'),
        # Salesforce configuration (Pattern 2 - External Services via AgentCore Identity Token Exchange)
        'SALESFORCE_INSTANCE_URL': SALESFORCE_INSTANCE_URL,
        'SALESFORCE_PROVIDER_NAME': SALESFORCE_PROVIDER_NAME,
        # Role assumed per-request via AssumeRoleWithWebIdentity for
        # department-scoped DynamoDB access. The agent exchanges the user's
        # ID token for credentials scoped by the department session tag.
        'USER_SCOPED_DYNAMODB_ROLE_ARN': user_scoped_role_arn,
    }

    print(f"\nDeploying '{agent_name}'...")
    print(f"  Role: {role_arn}")
    print(f"  customClaims: department == {department}")

    # Forward the user's Cognito ID token to the agent as a custom header.
    # AgentCore strips the Authorization header, so the ID token (needed for
    # AssumeRoleWithWebIdentity -> department-scoped DynamoDB) is passed via
    # this allowlisted header. 'X-Id-Token' is permitted: it is not a
    # restricted header and does not use the reserved x-amz-/x-amzn- prefixes.
    request_header_config = {
        'requestHeaderAllowlist': ['X-Id-Token']
    }

    try:
        response = client.create_agent_runtime(
            agentRuntimeName=agent_name,
            agentRuntimeArtifact={
                'containerConfiguration': {
                    'containerUri': image_uri
                }
            },
            authorizerConfiguration=authorizer_config,
            requestHeaderConfiguration=request_header_config,
            networkConfiguration={"networkMode": "PUBLIC"},
            roleArn=role_arn,
            environmentVariables=env_vars,
            lifecycleConfiguration={
                'idleRuntimeSessionTimeout': 300,
                'maxLifetime': 1800,
            }
        )
        print(f"  Created: {response['agentRuntimeArn']}")
        return response

    except client.exceptions.ConflictException:
        print(f"  Agent '{agent_name}' already exists. Updating...")
        list_resp = client.list_agent_runtimes()
        agent_id = None
        # Try both possible response keys
        runtimes = list_resp.get('agentRuntimeSummaries', []) or list_resp.get('agentRuntimes', [])
        for a in runtimes:
            if a.get('agentRuntimeName') == agent_name:
                agent_id = a.get('agentRuntimeId') or a.get('agentRuntimeArn')
                break
        if agent_id:
            # Use agentRuntimeId for update (not ARN).
            # update_agent_runtime is a full PUT — all required fields must be
            # included even if unchanged, including requestHeaderConfiguration.
            response = client.update_agent_runtime(
                agentRuntimeId=agent_id if not agent_id.startswith('arn:') else agent_id.split('/')[-1],
                agentRuntimeArtifact={
                    'containerConfiguration': {
                        'containerUri': image_uri
                    }
                },
                authorizerConfiguration=authorizer_config,
                requestHeaderConfiguration=request_header_config,
                networkConfiguration={"networkMode": "PUBLIC"},
                roleArn=role_arn,
                environmentVariables=env_vars,
            )
            print(f"  Updated: {agent_id}")
            return response
        raise


def main():
    parser = argparse.ArgumentParser(
        description='Deploy per-department CustomerHub agents to AgentCore'
    )
    parser.add_argument('--env', default='dev', choices=['dev', 'prod'])
    parser.add_argument('--skip-build', action='store_true',
                        help='Skip Docker build, use existing image')
    parser.add_argument('--department', choices=DEPARTMENTS,
                        help='Deploy only one department (default: all three)')
    args = parser.parse_args()

    if not COGNITO_USER_POOL_ID:
        print("Error: COGNITO_USER_POOL_ID not set in .env file.")
        sys.exit(1)

    # Get stack outputs for role ARNs and resource names
    outputs = get_stack_outputs(args.env)
    table_name = outputs.get('CustomerHubDataTableName', f'CustomerHubData-{args.env}')
    bucket_name = outputs.get('KnowledgeBaseBucketName', '')
    kb_id = os.getenv('KNOWLEDGE_BASE_ID', '')

    role_map = {
        'Sales': outputs.get('SalesAgentRoleArn', ''),
        'Finance': outputs.get('FinanceAgentRoleArn', ''),
    }

    # Shared role assumed per-request for department-scoped DynamoDB access.
    user_scoped_role_arn = outputs.get('UserScopedDynamoDBRoleArn', '')
    if not user_scoped_role_arn:
        print("\nWARNING: UserScopedDynamoDBRoleArn not found in stack outputs. "
              "Deploy the updated CloudFormation stack first, or the agent "
              "will be unable to access DynamoDB.")

    # Build and push the shared container image
    repo_name = f"customerhub-agent-{args.env}"
    repo_uri = create_ecr_repository(repo_name)

    if not args.skip_build:
        image_uri = build_and_push_image(repo_uri)
    else:
        image_uri = f"{repo_uri}:latest"
        print(f"Using existing image: {image_uri}")

    # Deploy agents
    departments = [args.department] if args.department else DEPARTMENTS
    results = {}

    for dept in departments:
        role_arn = role_map.get(dept)
        if not role_arn:
            print(f"\nWARNING: No role ARN found for {dept}. "
                  f"Check CloudFormation output '{dept}AgentRoleArn'.")
            continue
        resp = deploy_department_agent(
            department=dept,
            image_uri=image_uri,
            role_arn=role_arn,
            env=args.env,
            table_name=table_name,
            bucket_name=bucket_name,
            kb_id=kb_id,
            user_scoped_role_arn=user_scoped_role_arn,
        )
        results[dept] = resp.get('agentRuntimeArn', 'N/A')

    # Print .env values
    print("\n" + "=" * 60)
    print("Deployment complete. Add these to your .env file:")
    print("=" * 60)
    for dept, arn in results.items():
        print(f"AGENTCORE_AGENT_ARN_{dept.upper()}={arn}")


if __name__ == '__main__':
    main()
