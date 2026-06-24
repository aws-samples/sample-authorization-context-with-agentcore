"""
AgentCore Runtime invocation module.

Invokes the AgentCore Runtime agent using JWT Bearer Token authentication.
The agent is configured to accept Cognito JWT tokens for authorization.
"""

import json
import logging
import os
import requests

logger = logging.getLogger(__name__)

AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')


class AgentCoreClient:
    def __init__(self, agent_arn: str, region: str):
        """
        Initialize AgentCore client.
        
        Args:
            agent_arn: Full ARN of the AgentCore Runtime agent
            region: AWS region
        """
        self.agent_arn = agent_arn
        self.region = region
        # Extract agent ID from ARN: arn:aws:bedrock-agentcore:region:account:runtime/agent-id
        self.agent_id = agent_arn.split('/')[-1] if agent_arn else ''
        
        # AgentCore Runtime endpoint
        self.endpoint = f"https://bedrock-agentcore.{region}.amazonaws.com"

    def invoke(self, user_input: str, session_id: str,
               access_token: str, department: str, id_token: str = "") -> str:
        """
        Invoke the AgentCore Runtime agent with JWT bearer token.

        Args:
            user_input: The user's message/query
            session_id: Session identifier for conversation continuity
            access_token: Cognito access token (used as Bearer token —
                          contains the 'department' custom claim injected
                          by the V2 Pre-Token Generation trigger.
                          AgentCore's customJWTAuthorizer validates it
                          against the configured discoveryUrl,
                          allowedAudience, and customClaims rules.)
            department: User's department (for logging/display)
            id_token: Cognito ID token. AgentCore strips the Authorization
                      header before forwarding to the container, so the ID
                      token is passed in sessionAttributes. The agent
                      exchanges it via AssumeRoleWithWebIdentity for
                      department-scoped DynamoDB credentials (the ID token
                      carries the 'aud' and 'https://aws.amazon.com/tags'
                      claims STS requires).

        Returns:
            Agent response text
        """
        logger.info("Invoking agent for department: %s, session: %s",
                     department, session_id)

        # Build the payload - match the agent_server.py expected format.
        # id_token is intentionally NOT in sessionAttributes — it is sent only
        # via the X-Id-Token header. This verifies the header path works end to
        # end. (The agent still supports a sessionAttributes fallback if ever
        # needed.)
        payload = {
            "input": {
                "prompt": user_input,
                "sessionAttributes": {
                    "department": department
                }
            }
        }

        # URL encode the agent ARN for the path
        import urllib.parse
        encoded_arn = urllib.parse.quote(self.agent_arn, safe='')
        
        # Ensure session ID meets minimum length (33+ chars)
        padded_session_id = self._ensure_session_id_length(session_id)
        
        # Construct the URL with qualifier parameter
        url = f"{self.endpoint}/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
        
        # Set headers with JWT Bearer token and session ID.
        # X-Id-Token carries the Cognito ID token to the agent for
        # AssumeRoleWithWebIdentity (department-scoped DynamoDB). It is an
        # allowlisted custom header on the runtime (requestHeaderConfiguration);
        # AgentCore forwards it to the container even though it strips
        # Authorization. The ID token is also kept in sessionAttributes as a
        # backward-compatible fallback.
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": padded_session_id
        }
        if id_token:
            headers["X-Id-Token"] = id_token

        try:
            logger.info("Calling AgentCore Runtime: %s", url)
            response = requests.post(url, json=payload, headers=headers, timeout=60)
            
            if response.status_code == 200:
                result = self._parse_response(response.json())
                logger.info("Agent response received for session: %s", session_id)
                return result
            else:
                error_msg = f"Agent returned status {response.status_code}: {response.text}"
                logger.error(error_msg)
                return f"Error: {error_msg}"

        except requests.exceptions.Timeout:
            logger.error("Agent invocation timed out")
            return "Error: Request timed out. Please try again."
        except Exception as e:
            logger.error("Agent invocation failed: %s", str(e))
            return f"Error: Unable to get response from agent. {str(e)}"

    def _ensure_session_id_length(self, session_id: str) -> str:
        """Ensure session ID meets minimum length requirement (33+ chars)."""
        if len(session_id) < 33:
            return session_id + "0" * (33 - len(session_id))
        return session_id

    def _parse_response(self, response_data: dict) -> str:
        """Parse the agent response."""
        try:
            output = response_data.get('output', {})
            message = output.get('message', '')
            
            # Handle different message formats
            if isinstance(message, dict):
                # Strands agent format
                content = message.get('content', [])
                if content and isinstance(content, list):
                    texts = [c.get('text', '') for c in content if isinstance(c, dict)]
                    return '\n'.join(texts) if texts else str(message)
                return str(message)
            
            return message if message else json.dumps(response_data)
            
        except Exception as e:
            logger.error("Failed to parse response: %s", str(e))
            return f"Error parsing response: {str(e)}"
