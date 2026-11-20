#!/usr/bin/env python3
"""
One-time script to set up Google OAuth2 credentials.

Run this on a machine with a browser:
    python scripts/setup_google_oauth.py

It will:
1. Open your browser for Google login
2. Ask you to grant Calendar access
3. Save the token to credentials/token.json

After this, copy credentials/token.json to your VPS.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CREDENTIALS_PATH = os.environ.get("GOOGLE_CREDENTIALS_PATH", "credentials/google_credentials.json")
TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "credentials/token.json")


def main():
    if not os.path.exists(CREDENTIALS_PATH):
        print(f"ERROR: Google credentials file not found: {CREDENTIALS_PATH}")
        print()
        print("To set up:")
        print("1. Go to https://console.cloud.google.com/")
        print("2. Create a project (or select existing)")
        print("3. Enable 'Google Calendar API'")
        print("4. Go to APIs & Services -> Credentials")
        print("5. Create OAuth client ID (type: Desktop app)")
        print("6. Download JSON and save as credentials/google_credentials.json")
        sys.exit(1)

    print("Starting Google OAuth2 authorization flow...")
    print("A browser window will open. Log in with your Google account.")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
    creds = flow.run_local_server(port=0)

    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())

    print()
    print(f"Success! Token saved to {TOKEN_PATH}")
    print("You can now copy this file to your VPS.")


if __name__ == "__main__":
    main()
