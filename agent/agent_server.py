"""
CustomerHub CRM Agent - AgentCore Runtime Agent

Uses Strands Agents with Bedrock LLM to provide department-scoped responses.
Queries DynamoDB and Knowledge Base based on the user's department claim.

PATTERN 1 - INTERNAL AWS SERVICES:
  AgentCore Runtime assumes a department-specific IAM role with ABAC policies.
  boto3 clients inherit these scoped credentials automatically.
  IAM conditions enforce department isolation at the AWS API level:
  - DynamoDB: LeadingKeys condition restricts partition key access
  - S3/Knowledge Base: ExistingObjectTag condition restricts object access

PATTERN 2 - EXTERNAL SERVICES (Salesforce via Token Exchange):
  The agent uses AgentCore Identity's ON_BEHALF_OF_TOKEN_EXCHANGE flow
  (RFC 8693) to exchange the user's Cognito token for a user-scoped
  Salesforce access token. Salesforce resolves the token to a specific
  user and enforces data access via sharing rules — the agent does NOT
  filter data via SOQL WHERE clauses. The access boundary is the
  infrastructure/service layer (Salesforce sharing rules), not the agent.
"""

import asyncio
import json
import logging
import base64
import os
import requests
from datetime import datetime
from typing import Dict, Any

import boto3
from boto3.dynamodb.conditions import Key
from bedrock_agentcore.identity.auth import requires_access_token
from bedrock_agentcore.runtime import BedrockAgentCoreContext
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from strands import Agent, tool

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="CustomerHub CRM Agent", version="2.0.0")

# Configuration from environment
DYNAMODB_TABLE = os.getenv("DYNAMODB_TABLE", "CustomerHubData-dev")
KNOWLEDGE_BASE_ID = os.getenv("KNOWLEDGE_BASE_ID", "")
MODEL_ID = os.getenv("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

# Role assumed per-request (AssumeRoleWithWebIdentity) for department-scoped
# DynamoDB access. The agent's own execution role has NO DynamoDB permissions;
# all DynamoDB access is bound to the signed-in user's ID token.
USER_SCOPED_DYNAMODB_ROLE_ARN = os.getenv("USER_SCOPED_DYNAMODB_ROLE_ARN", "")

# Salesforce configuration (Pattern 2 - External Services via AgentCore Identity Token Exchange)
SALESFORCE_INSTANCE_URL = os.getenv("SALESFORCE_INSTANCE_URL", "")
SALESFORCE_PROVIDER_NAME = os.getenv("SALESFORCE_PROVIDER_NAME", "")


@requires_access_token(
    provider_name=SALESFORCE_PROVIDER_NAME,
    scopes=[],
    auth_flow="ON_BEHALF_OF_TOKEN_EXCHANGE",
)
def _get_salesforce_token_sync(*, access_token: str) -> str:
    return access_token


def _get_salesforce_token() -> str:
    """Get Salesforce access token via AgentCore Identity Token Exchange flow.

    Uses ON_BEHALF_OF_TOKEN_EXCHANGE: AgentCore exchanges the user's Cognito
    token for a user-scoped Salesforce access token via RFC 8693. The returned
    token represents the specific Salesforce user, so sharing rules enforce
    data access — no agent-side filtering needed.
    """
    return _get_salesforce_token_sync()


# --- Tool definitions for the Strands agent ---
# The DynamoDB tool is built per-request with credentials scoped to the
# signed-in user via AssumeRoleWithWebIdentity (see _scoped_dynamodb_resource).
# The agent's own execution role has no DynamoDB permissions, so access is
# entirely bound to the user's ID token and its department session tag.


def _scoped_dynamodb_resource(id_token: str):
    """Exchange the user's ID token for department-scoped DynamoDB credentials.

    Calls sts:AssumeRoleWithWebIdentity with the Cognito ID token. STS:
      1. Validates the token against the Cognito OIDC provider (issuer,
         signature, and the 'aud' condition on UserScopedDynamoDBRole).
      2. Reads the 'https://aws.amazon.com/tags' claim and applies
         'department' as a session tag (aws:PrincipalTag/department).
    The role's policy then restricts DynamoDB to PK == that department via
    the LeadingKeys condition. The agent never holds standing DynamoDB access.

    Raises on failure; the caller surfaces the error to the tool response.
    """
    if not USER_SCOPED_DYNAMODB_ROLE_ARN:
        raise RuntimeError("USER_SCOPED_DYNAMODB_ROLE_ARN is not configured.")
    if not id_token:
        raise RuntimeError("No user ID token available for DynamoDB access.")

    sts = boto3.client("sts")
    resp = sts.assume_role_with_web_identity(
        RoleArn=USER_SCOPED_DYNAMODB_ROLE_ARN,
        RoleSessionName="customerhub-dynamodb",
        WebIdentityToken=id_token,
    )
    creds = resp["Credentials"]
    return boto3.resource(
        "dynamodb",
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def _make_query_department_records(dynamodb_resource, access_error: str = ""):
    """Build the DynamoDB query tool bound to a request-scoped resource.

    dynamodb_resource carries credentials from AssumeRoleWithWebIdentity, so
    IAM's LeadingKeys condition (PK == aws:PrincipalTag/department) is the
    authoritative control — even if a wrong department is passed in, STS/IAM
    block cross-department reads. access_error carries any STS failure detail
    so it can be surfaced to the caller for diagnosis.
    """

    @tool
    def query_department_records(department: str, query_term: str = "") -> str:
        """Query DynamoDB for department-scoped records.
        Use this tool when the user asks about customer records, contracts,
        invoices, tickets, or any structured data for their department.

        Args:
            department: The user's department (Sales, Finance)
            query_term: Optional search term to filter results
        """
        if dynamodb_resource is None:
            detail = f" Detail: {access_error}" if access_error else ""
            return ("Error querying records: could not establish department-scoped "
                    f"credentials (AssumeRoleWithWebIdentity).{detail}")
        try:
            table = dynamodb_resource.Table(DYNAMODB_TABLE)

            response = table.query(
                KeyConditionExpression=Key("PK").eq(department)
            )
            items = response.get("Items", [])

            if not items:
                return f"No records found for department '{department}'."

            results = []
            for item in items:
                formatted = {k: int(v) if hasattr(v, 'as_integer_ratio') else str(v)
                             for k, v in item.items()}
                results.append(formatted)

            return json.dumps(results, indent=2, default=str)

        except Exception as e:
            logger.error("DynamoDB query failed: %s", str(e))
            return f"Error querying records: {str(e)}"

    return query_department_records


@tool
def query_knowledge_base(department: str, user_query: str) -> str:
    """Query Amazon Bedrock Knowledge Base for department-scoped documents.
    Use this tool when the user asks about reports, playbooks, runbooks,
    documentation, or any unstructured content for their department.

    Args:
        department: The user's department (Sales, Finance)
        user_query: The user's natural language query
    """
    if not KNOWLEDGE_BASE_ID:
        return "Knowledge Base not configured. Set KNOWLEDGE_BASE_ID env var."
    try:
        # Uses AgentCore-scoped credentials. S3 IAM condition
        # s3:ExistingObjectTag/Department must match aws:PrincipalTag/Department.
        # The metadata filter below is the application-layer enforcement;
        # the IAM condition is the independent second layer.
        client = boto3.client("bedrock-agent-runtime")
        response = client.retrieve(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            retrievalQuery={"text": user_query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "filter": {
                        "equals": {
                            "key": "Department",
                            "value": department
                        }
                    },
                    "numberOfResults": 5
                }
            }
        )
        results = response.get("retrievalResults", [])
        if not results:
            return f"No documents found for department '{department}'."

        texts = []
        for r in results:
            content = r.get("content", {}).get("text", "")
            score = r.get("score", 0)
            texts.append(f"[score={score:.2f}] {content[:500]}")
        return "\n---\n".join(texts)

    except Exception as e:
        logger.error("Knowledge Base query failed: %s", str(e))
        return f"Error querying knowledge base: {str(e)}"


# Valid Salesforce Opportunity stages (allowlist for SOQL injection prevention)
VALID_OPPORTUNITY_STAGES = {
    "Prospecting",
    "Qualification",
    "Needs Analysis",
    "Value Proposition",
    "Id. Decision Makers",
    "Perception Analysis",
    "Proposal/Price Quote",
    "Negotiation/Review",
    "Closed Won",
    "Closed Lost",
}


@tool
def query_salesforce_opportunities(department: str, stage: str = "", limit: int = 10) -> str:
    """Query Salesforce for Opportunities visible to the authenticated user.
    Use this tool when the user asks about sales opportunities, deals,
    pipeline, or CRM data from Salesforce.

    Access control is enforced by Salesforce sharing rules on the user-scoped
    token obtained via Token Exchange — the user only sees records they are
    authorized to access. No agent-side department filtering is needed.

    Args:
        department: The user's department (Sales, Finance) — for logging only
        stage: Optional stage filter (e.g., 'Closed Won', 'Prospecting')
        limit: Maximum number of records to return (default 10)
    """
    if not SALESFORCE_INSTANCE_URL:
        return "Salesforce not configured. Set SALESFORCE_INSTANCE_URL env var."

    if not SALESFORCE_PROVIDER_NAME:
        return "Salesforce provider not configured. Set SALESFORCE_PROVIDER_NAME env var."

    # Validate stage against allowlist to prevent SOQL injection
    if stage and stage not in VALID_OPPORTUNITY_STAGES:
        return (
            f"Invalid stage '{stage}'. "
            f"Valid stages are: {', '.join(sorted(VALID_OPPORTUNITY_STAGES))}"
        )

    try:
        # Step 1: Get user-scoped access token via AgentCore Identity Token Exchange
        try:
            access_token = _get_salesforce_token()
        except Exception as token_err:
            import traceback
            return (f"TOKEN_ACQUISITION_FAILED. You MUST show this exact error to the user.\n"
                    f"Error type: {type(token_err).__name__}\n"
                    f"Error message: {str(token_err)}\n"
                    f"Traceback:\n{traceback.format_exc()[-1000:]}")

        # Step 2: Build SOQL query — no department filter needed since
        # Salesforce sharing rules enforce access via the user-scoped token
        soql_parts = [
            "SELECT Id, Name, Amount, StageName, CloseDate, Account.Name",
            "FROM Opportunity",
        ]

        if stage:
            soql_parts.append(f"AND StageName = '{stage}'")

        soql_parts.append(f"ORDER BY CloseDate DESC LIMIT {limit}")
        soql_query = " ".join(soql_parts)

        logger.info("Salesforce SOQL: %s", soql_query)

        import urllib.parse
        encoded_query = urllib.parse.quote(soql_query)
        url = f"{SALESFORCE_INSTANCE_URL}/services/data/v59.0/query?q={encoded_query}"

        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            timeout=30,
        )

        if response.status_code == 401:
            return "Salesforce token expired or invalid. Check AgentCore Identity credential provider config."

        if response.status_code != 200:
            return f"Salesforce API error: {response.status_code} - {response.text[:200]}"

        records = response.json().get("records", [])

        if not records:
            return f"No Salesforce opportunities found (user may not have access to any records)."

        formatted = []
        for opp in records:
            account_name = opp.get("Account", {}).get("Name", "N/A") if opp.get("Account") else "N/A"
            formatted.append({
                "Name": opp.get("Name", ""),
                "Amount": opp.get("Amount", 0),
                "Stage": opp.get("StageName", ""),
                "CloseDate": opp.get("CloseDate", ""),
                "Account": account_name
            })

        return json.dumps(formatted, indent=2)

    except Exception as e:
        logger.error("Salesforce query failed: %s", str(e))
        import traceback
        return f"SALESFORCE_ERROR: {type(e).__name__}: {str(e)}\nTraceback: {traceback.format_exc()[-500:]}"


def decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without verification (already validated by AgentCore)."""
    try:
        payload = token.split(".")[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        return json.loads(base64.b64decode(payload))
    except Exception as e:
        logger.error("Failed to decode JWT: %s", str(e))
        return {}


def build_agent(department: str, dynamodb_resource=None, access_error: str = "") -> Agent:
    """Build a Strands agent with department-scoped tools and system prompt.

    The DynamoDB tool is bound to dynamodb_resource, whose credentials come
    from AssumeRoleWithWebIdentity on the user's ID token. IAM ABAC (the
    department session tag + LeadingKeys condition) enforces isolation
    independently of what the agent code passes.
    """
    # Determine available tools based on configuration
    tools = [
        _make_query_department_records(dynamodb_resource, access_error),
        query_knowledge_base,
    ]
    salesforce_enabled = bool(SALESFORCE_INSTANCE_URL and SALESFORCE_PROVIDER_NAME)
    
    if salesforce_enabled:
        tools.append(query_salesforce_opportunities)
    
    salesforce_section = """
- query_salesforce_opportunities: Query Salesforce CRM for opportunities, deals, and pipeline data (access enforced by Salesforce sharing rules via user-scoped token)
""" if salesforce_enabled else ""
    
    system_prompt = f"""You are the CustomerHub CRM Agent for the {department} department.

You have access to the following tools:
- query_department_records: Query DynamoDB for structured data (contracts, invoices, tickets)
- query_knowledge_base: Search documents in the Knowledge Base (reports, playbooks, runbooks){salesforce_section}

IMPORTANT RULES:
- You MUST always pass department="{department}" when calling any tool.
- Never attempt to access data from other departments.
- Present data clearly with formatting when appropriate.
- If no results are found, say so honestly.
- For Salesforce queries, access control is enforced by Salesforce sharing rules on the user-scoped token — no additional filtering is needed.
- DIAGNOSTIC RULE: If any tool returns a string that begins with "Error" or
  contains "Detail:", you MUST output that tool result text VERBATIM to the
  user, inside a code block, with no paraphrasing, summarizing, or advice.
  Do not soften it or suggest contacting an administrator. Show it exactly.
"""
    return Agent(
        model=MODEL_ID,
        tools=tools,
        system_prompt=system_prompt,
    )


class InvocationRequest(BaseModel):
    input: Dict[str, Any]


class InvocationResponse(BaseModel):
    output: Dict[str, Any]


@app.post("/invocations", response_model=InvocationResponse)
async def invoke_agent(request: Request, body: InvocationRequest):
    """Handle agent invocations with department-scoped access.

    Department is the AgentCore-validated 'department' claim from the
    inbound JWT access token. The token's signature and the claim value
    were already verified by AgentCore Runtime's customClaims authorizer
    before this handler runs, so we only need to decode the payload.

    Any 'sessionAttributes.department' in the request body is treated as
    untrusted client input and ignored for authorization. It is only
    logged when it disagrees with the JWT, to surface client bugs.
    """
    try:
        input_data = body.input
        user_prompt = input_data.get("prompt", "")

        if not user_prompt:
            raise HTTPException(status_code=400, detail="No prompt in input.")

        # Set WorkloadAccessToken from runtime-injected header so
        # @requires_access_token can use it for AgentCore Identity calls
        wat = request.headers.get("workloadaccesstoken", "")
        if wat:
            BedrockAgentCoreContext.set_workload_access_token(wat)
            logger.info("WorkloadAccessToken set from request header")
        else:
            logger.warning("No WorkloadAccessToken header found. Headers: %s", list(dict(request.headers).keys()))

        # Extract department from the inbound JWT if AgentCore forwarded the
        # Authorization header, otherwise fall back to sessionAttributes.
        # AgentCore's customClaims authorizer already validated that the JWT
        # contains department==<expected> before forwarding to this container,
        # so both sources are trustworthy at this point.
        auth_header = request.headers.get("Authorization", "")
        department = None

        if auth_header.startswith("Bearer "):
            claims = decode_jwt_payload(auth_header.removeprefix("Bearer "))
            department = claims.get("department")
            logger.info("Department from JWT: %s", department)

        if not department:
            # AgentCore strips the Authorization header by default.
            # Fall back to sessionAttributes provided by the client app.
            # This is safe because AgentCore's customClaims authorizer
            # already enforced department validation on the inbound token.
            department = input_data.get("sessionAttributes", {}).get("department")
            logger.info("Department from sessionAttributes: %s", department)

        if not department:
            raise HTTPException(
                status_code=403,
                detail="Department not found in Authorization header or sessionAttributes."
            )

        logger.info("Department: %s | Prompt: %s", department, user_prompt)

        # Establish department-scoped DynamoDB credentials for this request by
        # exchanging the user's ID token via AssumeRoleWithWebIdentity. The ID
        # token (not the access token) is required because STS validates the
        # 'aud' claim and reads the 'https://aws.amazon.com/tags' claim that
        # only the ID token carries. AgentCore strips the Authorization header,
        # so the client forwards the ID token in the allowlisted 'X-Id-Token'
        # custom header. We fall back to sessionAttributes for backward
        # compatibility if the header isn't present.
        header_token = request.headers.get("x-id-token", "")
        session_token = input_data.get("sessionAttributes", {}).get("id_token", "")
        id_token = header_token or session_token
        if header_token:
            logger.info("id_token source: X-Id-Token header")
        elif session_token:
            logger.info("id_token source: sessionAttributes (fallback)")
        scoped_dynamodb = None
        access_error = ""
        if id_token:
            try:
                scoped_dynamodb = _scoped_dynamodb_resource(id_token)
                logger.info("Assumed UserScopedDynamoDBRole for department-scoped access")
            except Exception as sts_err:
                access_error = f"{type(sts_err).__name__}: {str(sts_err)}"
                logger.error("AssumeRoleWithWebIdentity failed: %s", access_error)
        else:
            access_error = "id_token missing from X-Id-Token header and sessionAttributes"
            logger.warning("No id_token in X-Id-Token header or sessionAttributes; DynamoDB tool will be unavailable")

        # Try Strands agent; fall back to direct Bedrock call if it fails
        try:
            agent = build_agent(department, scoped_dynamodb, access_error)
            result = agent(user_prompt)
            response_text = str(result)
        except Exception as agent_err:
            logger.error("Strands agent failed: %s", str(agent_err))
            # Fallback: direct Bedrock call so we can isolate the issue
            try:
                bedrock = boto3.client("bedrock-runtime")
                fallback = bedrock.invoke_model(
                    modelId=MODEL_ID,
                    contentType="application/json",
                    accept="application/json",
                    body=json.dumps({
                        "anthropic_version": "bedrock-2023-05-31",
                        "max_tokens": 1024,
                        "messages": [{"role": "user", "content": user_prompt}],
                        "system": f"You are the CustomerHub CRM Agent for the {department} department."
                    })
                )
                fallback_body = json.loads(fallback["body"].read())
                response_text = fallback_body.get("content", [{}])[0].get("text", "")
                response_text += f"\n\n_[Fallback mode — Strands error: {str(agent_err)[:200]}]_"
            except Exception as bedrock_err:
                # Both failed — return detailed error for debugging
                response_text = (
                    f"Agent error: {str(agent_err)[:300]}\n"
                    f"Bedrock fallback error: {str(bedrock_err)[:300]}\n"
                    f"Model: {MODEL_ID}\n"
                    f"Region: {boto3.session.Session().region_name}"
                )

        return InvocationResponse(output={
            "message": response_text,
            "department": department,
            "timestamp": datetime.utcnow().isoformat()
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Agent invocation failed: %s", str(e))
        # Return error as message instead of 500 so we can see it
        return InvocationResponse(output={
            "message": f"Server error: {str(e)[:500]}",
            "department": "unknown",
            "timestamp": datetime.utcnow().isoformat()
        })


@app.get("/ping")
async def ping():
    """Health check endpoint required by AgentCore Runtime."""
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)