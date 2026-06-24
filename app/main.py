"""
CustomerHub CRM - Streamlit Application

A POC demonstrating department-based authorization for AI agents
using Amazon Cognito custom claims and Amazon Bedrock AgentCore Identity.
"""

import streamlit as st
import uuid
import os
from dotenv import load_dotenv
from auth import CognitoAuth
from agent import AgentCoreClient

# Load environment variables
load_dotenv()

# Configuration
AWS_REGION = os.getenv('AWS_REGION', 'us-east-1')
COGNITO_USER_POOL_ID = os.getenv('COGNITO_USER_POOL_ID', '')
COGNITO_APP_CLIENT_ID = os.getenv('COGNITO_APP_CLIENT_ID', '')

# Per-department agent ARNs — each points to a separate AgentCore Runtime
# with its own IAM role and customClaims authorizer.
AGENT_ARNS = {
    'Sales': os.getenv('AGENTCORE_AGENT_ARN_SALES', ''),
    'Finance': os.getenv('AGENTCORE_AGENT_ARN_FINANCE', ''),
}

# Initialize auth client (shared across departments)
auth_client = CognitoAuth(COGNITO_USER_POOL_ID, COGNITO_APP_CLIENT_ID, AWS_REGION)

# Pre-build an AgentCoreClient per department
agent_clients = {
    dept: AgentCoreClient(arn, AWS_REGION)
    for dept, arn in AGENT_ARNS.items() if arn
}

# Page config
st.set_page_config(
    page_title="CustomerHub CRM",
    page_icon="🏢",
    layout="wide"
)


def _set_authenticated(result):
    """Store auth result in session state."""
    st.session_state['authenticated'] = True
    st.session_state['id_token'] = result['id_token']
    st.session_state['access_token'] = result['access_token']
    st.session_state['department'] = result['department']
    st.session_state['email'] = result['email']
    st.session_state['claims'] = result['claims']
    st.session_state['id_claims'] = result.get('id_claims', {})
    st.session_state['access_claims'] = result.get('access_claims', {})
    st.session_state['session_id'] = str(uuid.uuid4())
    st.session_state['messages'] = []
    st.rerun()


def _clear_challenge_state():
    """Remove all challenge-related keys from session state."""
    for key in ['password_change_required', 'challenge_session',
                'challenge_email']:
        st.session_state.pop(key, None)


def show_login():
    """Render the login form."""
    st.title("🏢 CustomerHub CRM")
    st.subheader("Sign in to access your department data")

    with st.form("login_form"):
        email = st.text_input("Email", placeholder="[email]")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign In", use_container_width=True)

        if submitted:
            if not email or not password:
                st.error("Please enter both email and password.")
                return

            with st.spinner("Authenticating..."):
                result = auth_client.login(email, password)

            if result['success']:
                _set_authenticated(result)
            elif result.get('challenge') == 'NEW_PASSWORD_REQUIRED':
                st.session_state['password_change_required'] = True
                st.session_state['challenge_session'] = result['session']
                st.session_state['challenge_email'] = result['email']
                st.rerun()
            else:
                st.error(result['error'])


def show_sidebar():
    """Render sidebar with user info and token details."""
    with st.sidebar:
        st.markdown("### 👤 User Info")
        st.write(f"**Email:** {st.session_state['email']}")
        st.write(f"**Department:** {st.session_state['department'].upper()}")
        st.write(f"**Session:** {st.session_state['session_id'][:8]}...")

        st.divider()

        with st.expander("🔑 JWT Claims"):
            id_claims = st.session_state.get('id_claims', {})
            access_claims = st.session_state.get('access_claims', {})
            st.caption(
                "department appears in both tokens via the V2 Pre-Token "
                "Generation trigger. AgentCore validates the access token."
            )
            cols = st.columns(2)
            with cols[0]:
                st.markdown("**ID Token**")
                st.code(f"department: {id_claims.get('department', 'N/A')}")
                st.code(f"email:      {id_claims.get('email', 'N/A')}")
                st.code(f"token_use:  {id_claims.get('token_use', 'N/A')}")
                st.code(f"aud:        {id_claims.get('aud', 'N/A')}")
            with cols[1]:
                st.markdown("**Access Token**")
                st.code(f"department: {access_claims.get('department', 'N/A')}")
                st.code(f"client_id:  {access_claims.get('client_id', 'N/A')}")
                st.code(f"token_use:  {access_claims.get('token_use', 'N/A')}")
                st.code(f"scope:      {access_claims.get('scope', 'N/A')}")

        with st.expander("🪙 Raw ID Token"):
            st.code(st.session_state.get('id_token', ''), language=None)
            st.caption("Copy and paste into jwt.io to inspect the full token.")

        with st.expander("🔐 Raw Access Token (sent to AgentCore)"):
            st.code(st.session_state.get('access_token', ''), language=None)
            st.caption(
                "This is the bearer token AgentCore validates against its "
                "customJWTAuthorizer (discoveryUrl, allowedAudience, "
                "customClaims)."
            )

        with st.expander("📋 All Decoded Claims"):
            st.markdown("**ID Token**")
            st.json(st.session_state.get('id_claims', {}))
            st.markdown("**Access Token**")
            st.json(st.session_state.get('access_claims', {}))

        st.divider()

        if st.button("🚪 Sign Out", use_container_width=True):
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            st.rerun()


def show_chat():
    """Render the chat interface."""
    department = st.session_state['department']
    dept_title = department.capitalize()
    st.title(f"🏢 CustomerHub CRM — {dept_title} Department")

    # Resolve the department-specific agent client
    agent_client = agent_clients.get(dept_title)
    if not agent_client:
        st.error(
            f"No agent configured for department '{dept_title}'. "
            f"Set AGENTCORE_AGENT_ARN_{department.upper()} in .env."
        )
        return

    for msg in st.session_state.get('messages', []):
        with st.chat_message(msg['role']):
            st.markdown(msg['content'])

    if prompt := st.chat_input(f"Ask about {department} data..."):
        st.session_state['messages'].append({'role': 'user', 'content': prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Retrieving department-scoped data..."):
                response = agent_client.invoke(
                    user_input=prompt,
                    session_id=st.session_state['session_id'],
                    access_token=st.session_state['access_token'],
                    department=department,
                    id_token=st.session_state['id_token']
                )
                st.markdown(response)

        st.session_state['messages'].append(
            {'role': 'assistant', 'content': response}
        )


def show_password_change():
    """Render the forced password change form for first-time login."""
    st.title("🏢 CustomerHub CRM")
    st.subheader("Set a new password")
    st.info("Your temporary password has expired. Please set a new password.")

    with st.form("password_change_form"):
        new_password = st.text_input("New Password", type="password")
        confirm_password = st.text_input("Confirm Password", type="password")
        submitted = st.form_submit_button("Set Password",
                                          use_container_width=True)

        if submitted:
            if not new_password or not confirm_password:
                st.error("Please fill in both fields.")
                return
            if new_password != confirm_password:
                st.error("Passwords do not match.")
                return

            with st.spinner("Updating password..."):
                result = auth_client.complete_new_password(
                    email=st.session_state['challenge_email'],
                    new_password=new_password,
                    session=st.session_state['challenge_session']
                )

            if result['success']:
                _clear_challenge_state()
                _set_authenticated(result)
            else:
                st.error(result['error'])


# --- Main App Flow ---
def main():
    if st.session_state.get('authenticated', False):
        show_sidebar()
        show_chat()
    elif st.session_state.get('password_change_required', False):
        show_password_change()
    else:
        show_login()


if __name__ == "__main__":
    main()
