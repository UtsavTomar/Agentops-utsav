import requests
import json

def create_sign_in_token(clerk_test_user, clerk_secret_key, clerk_api_base_url ,expires_in_seconds=1200):
    """
    Create a sign-in token for a user.
    
    Args:
        clerk_test_user: The ID of the user for whom to create the token.
        clerk_secret_key: Your Clerk Secret Key.
        expires_in_seconds: Token expiration time in seconds (default: 20 minutes).
        
    Returns:
        The sign-in token if successful, None otherwise.
    """
    import requests
    import json
    
    url = f"{clerk_api_base_url}/sign_in_tokens"
    
    headers = {
        "Authorization": f"Bearer {clerk_secret_key}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "user_id": clerk_test_user,
        "expires_in_seconds": expires_in_seconds
    }
    
    response = requests.post(url, headers=headers, json=payload)
    
    if response.status_code == 200:
        data = response.json()
        return data.get("token")
    else:
        #print(f"Failed to create sign-in token (HTTP {response.status_code}): {response.text}")
        return None


def exchange_token_for_jwt(signin_token, publishable_key, clerk_frontend_url):
    """
    Exchange a sign-in token for a JWT token.
    
    Args:
        signin_token: The sign-in token to exchange.
        publishable_key: The Clerk publishable key.
        clerk_frontend_url: The Clerk frontend URL.
        
    Returns:
        The JWT token if successful, None otherwise.
    """
    import requests
    import json
    
    url = f"{clerk_frontend_url}/client/sign_ins?strategy=ticket&ticket={signin_token}"
    
    headers = {
        "Content-Type": "application/json",
        "Clerk-Publishable-Key": publishable_key
    }

    response = requests.post(url, headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        
        if "client" in data and "sessions" in data["client"] and len(data["client"]["sessions"]) > 0:
            session = data["client"]["sessions"][0]
            return session["last_active_token"]["jwt"]
        else:
            print("Error: No session found in response")
            return None
    else:
        print(f"Error: {response.status_code} - {response.text}")
        return None


def generate_jwt_token(clerk_secret_key, clerk_publishable_key, clerk_test_user, clerk_frontend_url, clerk_api_base_url ,expires_in_seconds=1200):
    """
    Generate a JWT token for a user by first creating a sign-in token and then
    exchanging it for a JWT token.
    
    Args:
        clerk_secret_key: Your Clerk Secret Key.
        clerk_publishable_key: Your Clerk Publishable Key.
        clerk_test_user: The ID of the user for whom to create the token.
        clerk_frontend_url: The Clerk frontend URL.
        expires_in_seconds: Token expiration time in seconds (default: 20 minutes).
        
    Returns:
        The JWT token if successful, None otherwise.
    """
    # First generate a sign-in token
    #print("Generating sign-in token...")
    signin_token = create_sign_in_token(clerk_test_user, clerk_secret_key, clerk_api_base_url, expires_in_seconds)
    
    if not signin_token:
        print("Failed to generate sign-in token.")
        return None
    
    #print(f"Sign-in token generated: {signin_token[:10]}...{signin_token[-10:]}")
    
    # Then exchange it for a JWT token
    #print("Exchanging sign-in token for JWT token...")
    jwt_token = exchange_token_for_jwt(signin_token, clerk_publishable_key, clerk_frontend_url)
    
    if jwt_token:
        #print(f"JWT token generated: {jwt_token[:10]}...{jwt_token[-10:]}")
        return jwt_token
    else:
        print("Failed to exchange sign-in token for JWT token.")
        return None