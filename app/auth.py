"""
Cognito authentication module.

Handles user login via Cognito USER_PASSWORD_AUTH flow,
token retrieval, and JWT decoding to extract the department claim.
"""

import boto3
import json
import base64
import logging
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class CognitoAuth:
    def __init__(self, user_pool_id: str, client_id: str, region: str):
        self.user_pool_id = user_pool_id
        self.client_id = client_id
        self.region = region
        self.client = boto3.client('cognito-idp', region_name=region)

    def login(self, email: str, password: str) -> dict:
        """
        Authenticate user and return tokens with decoded claims.

        Returns:
            dict with keys: success, error, and on success:
                id_token, access_token, refresh_token, department, email,
                id_claims, access_claims, claims (alias of id_claims)
            Or if password change required:
                challenge, session (for completing the challenge)
        """
        try:
            response = self.client.initiate_auth(
                ClientId=self.client_id,
                AuthFlow='USER_PASSWORD_AUTH',
                AuthParameters={
                    'USERNAME': email,
                    'PASSWORD': password
                }
            )

            # Handle NEW_PASSWORD_REQUIRED challenge (first login)
            if response.get('ChallengeName') == 'NEW_PASSWORD_REQUIRED':
                return {
                    'success': False,
                    'challenge': 'NEW_PASSWORD_REQUIRED',
                    'session': response['Session'],
                    'email': email
                }

            return self._extract_tokens(response['AuthenticationResult'])

        except ClientError as e:
            error_code = e.response['Error']['Code']
            error_msg = e.response['Error']['Message']
            logger.error("Login failed: %s - %s", error_code, error_msg)
            return {
                'success': False,
                'error': self._friendly_error(error_code, error_msg)
            }
        except Exception as e:
            logger.error("Unexpected error during login: %s", str(e))
            return {
                'success': False,
                'error': 'An unexpected error occurred. Please try again.'
            }

    def complete_new_password(self, email: str, new_password: str,
                              session: str) -> dict:
        """
        Complete the NEW_PASSWORD_REQUIRED challenge.
        Called when a user logs in for the first time with a temporary password.
        """
        try:
            response = self.client.respond_to_auth_challenge(
                ClientId=self.client_id,
                ChallengeName='NEW_PASSWORD_REQUIRED',
                Session=session,
                ChallengeResponses={
                    'USERNAME': email,
                    'NEW_PASSWORD': new_password
                }
            )

            return self._extract_tokens(response['AuthenticationResult'])

        except ClientError as e:
            error_code = e.response['Error']['Code']
            error_msg = e.response['Error']['Message']
            logger.error("Password change failed: %s - %s",
                         error_code, error_msg)
            return {
                'success': False,
                'error': self._friendly_error(error_code, error_msg)
            }
        except Exception as e:
            logger.error("Unexpected error during password change: %s", str(e))
            return {
                'success': False,
                'error': 'An unexpected error occurred. Please try again.'
            }

    def _extract_tokens(self, auth_result: dict) -> dict:
        """Extract and decode tokens from Cognito auth result.

        The V2 Pre-Token Generation trigger injects 'department' into BOTH
        the ID token and access token. AgentCore's customJWTAuthorizer
        validates the access token, so 'department' for routing is sourced
        from the access token. 'email' lives only in the ID token.
        """
        id_token = auth_result['IdToken']
        access_token = auth_result['AccessToken']
        id_claims = self._decode_jwt(id_token)
        access_claims = self._decode_jwt(access_token)

        # Department comes from the access token — that's what AgentCore
        # actually validates against its customClaims authorizer.
        department = access_claims.get(
            'department', id_claims.get('department', 'unknown')
        )
        # Email is only in the ID token (Cognito access tokens don't
        # include user profile attributes by default).
        email = id_claims.get('email', '')

        return {
            'success': True,
            'id_token': id_token,
            'access_token': access_token,
            'refresh_token': auth_result.get('RefreshToken', ''),
            'department': department,
            'email': email,
            'id_claims': id_claims,
            'access_claims': access_claims,
            # Backwards-compatible alias — points to ID token claims.
            'claims': id_claims,
        }

    def _decode_jwt(self, token: str) -> dict:
        """
        Decode JWT payload without signature verification.
        Signature is verified by Cognito during authentication.
        In production with API Gateway, use a Cognito Authorizer
        for server-side validation.
        """
        try:
            payload = token.split('.')[1]
            # Add padding if needed
            padding = 4 - len(payload) % 4
            if padding != 4:
                payload += '=' * padding
            decoded = base64.b64decode(payload)
            return json.loads(decoded)
        except Exception as e:
            logger.error("Failed to decode JWT: %s", str(e))
            return {}

    def _friendly_error(self, error_code: str, error_msg: str) -> str:
        """Map Cognito error codes to user-friendly messages."""
        error_map = {
            'NotAuthorizedException': 'Invalid email or password.',
            'UserNotFoundException': 'No account found with this email.',
            'UserNotConfirmedException': 'Please confirm your account first.',
            'PasswordResetRequiredException': 'Password reset required.',
            'InvalidPasswordException': 'Password does not meet requirements. '
                'Must be 8+ characters with uppercase, lowercase, and numbers.',
        }
        return error_map.get(error_code, f'Login failed: {error_msg}')
