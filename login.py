"""
Daily login (SEBI requires 2FA every trading day).

Run each morning before the bot:
    python login.py
It opens the Zerodha login page. After logging in, copy the
`request_token` from the URL you're redirected to and paste it here.
The access token is saved to access_token.txt for bot.py.
"""
import webbrowser
from kiteconnect import KiteConnect
import config

kite = KiteConnect(api_key=config.API_KEY)
url = kite.login_url()
print("\nOpen this URL and log in:\n", url)
try:
    webbrowser.open(url)
except Exception:
    pass

token = input("\nPaste request_token from the redirect URL: ").strip()
data = kite.generate_session(token, api_secret=config.API_SECRET)
with open("access_token.txt", "w") as f:
    f.write(data["access_token"])
print("Login OK for", data.get("user_name", data.get("user_id")), "- token saved.")
