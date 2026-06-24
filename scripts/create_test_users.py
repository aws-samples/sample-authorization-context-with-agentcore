"""
Create test users in Cognito User Pool for each department.

Cognito sends a temporary password via email to each user.
Users must change their password on first login.

Usage:
    python scripts/create_test_users.py

Requires COGNITO_USER_POOL_ID and AWS_REGION in .env file.
"""

import boto3
import os
from dotenv import load_dotenv

load_dotenv()

USER_POOL_ID = os.getenv('COGNITO_USER_POOL_ID')
REGION = os.getenv('AWS_REGION', 'us-east-1')

# Test users — replace with real email addresses before running
TEST_USERS = [
    {
        'email': 'sales-user@example.com',
        'department': 'sales'
    },
    {
        'email': 'finance-user1@example.com',
        'department': 'finance'
    },
    {
        'email': 'finance-user2@example.com',
        'department': 'finance'
    },
]


def create_users():
    client = boto3.client('cognito-idp', region_name=REGION)

    for user in TEST_USERS:
        try:
            # Create user — Cognito generates a temporary password
            # and sends it to the user's email
            client.admin_create_user(
                UserPoolId=USER_POOL_ID,
                Username=user['email'],
                UserAttributes=[
                    {'Name': 'email', 'Value': user['email']},
                    {'Name': 'email_verified', 'Value': 'true'},
                    {'Name': 'custom:department', 'Value': user['department']},
                ],
                DesiredDeliveryMediums=['EMAIL']
                # No MessageAction='SUPPRESS' — Cognito sends the temp password
                # No TemporaryPassword — Cognito auto-generates one
                # User will be in FORCE_CHANGE_PASSWORD state until first login
            )

            print(f"Created user: {user['email']} "
                  f"(department: {user['department']}) "
                  f"— temporary password sent via email")

        except client.exceptions.UsernameExistsException:
            print(f"User already exists: {user['email']}")
        except Exception as e:
            print(f"Error creating {user['email']}: {str(e)}")


if __name__ == '__main__':
    if not USER_POOL_ID:
        print("Error: Set COGNITO_USER_POOL_ID in .env file first.")
        print("Deploy the CloudFormation stack and copy the output value.")
        exit(1)

    create_users()
    print("\nDone. Users will receive a temporary password via email.")
    print("They must change their password on first login.")
