"""
server — FastAPI HTTP service that wraps the existing zd-transfer
Python migration code as a long-running daemon for the Zendesk-app UI.

The CLI (`main.py`, `get_oauth_token.py`) is unchanged and remains the
operator's escape hatch. This package is purely additive.

Entry point:  `uvicorn server.app:app --host 0.0.0.0 --port 8080`
"""
