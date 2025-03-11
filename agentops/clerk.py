import requests
import boto3


class ClerkManager:
    def __init__(self, clerk_secret_key_param_name: str, clerk_test_user_param_name: str, clerk_api_base_url_param_name: str, clerk_publishable_key_param_name: str, clerk_frontend_url_param_name: str):
        """
        Initialize Clerk Token Manager with SSM parameter names.

        Args:
            clerk_secret_key_param_name (str): SSM parameter name for Clerk API key
            clerk_test_user_param_name (str): SSM parameter name for Clerk user ID
            clerk_api_base_url_param_name (str): SSM parameter name for Clerk API base URL
            clerk_publishable_key_param_name (str): SSM parameter name for Clerk publishable key
            clerk_frontend_url_param_name (str): SSM parameter name for Clerk frontend URL
        """
        self.clerk_secret_key_param_name = clerk_secret_key_param_name
        self.clerk_test_user_param_name = clerk_test_user_param_name
        self.clerk_api_base_url_param_name = clerk_api_base_url_param_name
        self.clerk_publishable_key_param_name = clerk_publishable_key_param_name
        self.clerk_frontend_url_param_name = clerk_frontend_url_param_name

        self.clerk_secret_key = None
        self.clerk_test_user = None
        self.clerk_api_base_url = None
        self.clerk_publishable_key = None
        self.clerk_frontend_url = None

    def get_ssm_parameter(self, param_name: str) -> str:
        """
        Get parameter from AWS SSM Parameter Store.

        Args:
            param_name (str): Parameter name in SSM

        Returns:
            str: Parameter value

        Raises:
            Exception: If parameter retrieval fails
        """
        try:
            ssm_client = boto3.client('ssm')
            response = ssm_client.get_parameter(
                Name=param_name,
                WithDecryption=True
            )
            return response['Parameter']['Value']
        except boto3.exceptions.Boto3Error as e:
            raise Exception(f"Failed to get parameter '{param_name}' from SSM: {str(e)}")

    def get_clerk_secret_key(self) -> str:
        """Retrieve the Clerk API secret key from AWS SSM."""
        if not self.clerk_secret_key:
            self.clerk_secret_key = self.get_ssm_parameter(self.clerk_secret_key_param_name)
        return self.clerk_secret_key

    def get_clerk_test_user(self) -> str:
        """Retrieve the Clerk test user ID from AWS SSM."""
        if not self.clerk_test_user:
            self.clerk_test_user = self.get_ssm_parameter(self.clerk_test_user_param_name)
        return self.clerk_test_user

    def get_clerk_api_base_url(self) -> str:
        """Retrieve the Clerk API base URL from AWS SSM."""
        if not self.clerk_api_base_url:
            self.clerk_api_base_url = self.get_ssm_parameter(self.clerk_api_base_url_param_name)
        return self.clerk_api_base_url

    def get_clerk_publishable_key(self) -> str:
        """Retrieve the Clerk publishable key from AWS SSM."""
        if not self.clerk_publishable_key:
            self.clerk_publishable_key = self.get_ssm_parameter(self.clerk_publishable_key_param_name)
        return self.clerk_publishable_key

    def get_clerk_frontend_url(self) -> str:
        """Retrieve the Clerk frontend URL from AWS SSM."""
        if not self.clerk_frontend_url:
            self.clerk_frontend_url = self.get_ssm_parameter(self.clerk_frontend_url_param_name)
        return self.clerk_frontend_url

    def get_all_clerk_variables(self):
        """
        Retrieve all Clerk-related variables at once.

        Returns:
            tuple: Clerk API key, test user ID, API base URL, publishable key, frontend URL
        """
        return (
            self.get_clerk_secret_key(),
            self.get_clerk_test_user(),
            self.get_clerk_api_base_url(),
            self.get_clerk_publishable_key(),
            self.get_clerk_frontend_url(),
        )
