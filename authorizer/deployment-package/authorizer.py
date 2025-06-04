import os, logging
import requests

log = logging.getLogger()
log.setLevel(logging.INFO)

ENDPOINT = os.environ["ENDPOINT"]


def _extract_token(event: dict) -> str | None:
    """Look for ?token=… in the query-string first, then Bearer header."""
    params = event.get("queryStringParameters") or {}
    if params.get("token"):
        return params["token"]

    headers = event.get("headers") or {}
    auth = headers.get("Authorization") or headers.get("authorization")
    if auth and auth.startswith("Bearer "):
        return auth.split(" ", 1)[1]

    return None


def handler(event, _ctx):
    token = _extract_token(event)
    if not token:
        return {"isAuthorized": False, "context": {"message": "token not found"}}

    try:
        # Instead of decoding JWT, make a request to the profile API
        profile_url = f"{ENDPOINT}/users/profile"
        headers = {"Authorization": f"Bearer {token}"}

        response = requests.get(profile_url, headers=headers)
        data = response.json()

        # Check if the response status is success
        if data.get("status") == "success" and "details" in data:
            principal = data["details"]["id"]

            return {
                "principalId": principal,
                "policyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Action": "execute-api:Invoke",
                            "Effect": "Allow",
                            "Resource": [
                                "arn:aws:execute-api:*:*:*/*"
                            ],  # allow this user on this API
                        }
                    ],
                },
                "context": {
                    "userId": principal,
                    "roles": ",".join(data["details"].get("roles", [])),
                },
            }
        else:
            raise ValueError("Invalid token or API response")

    except Exception as exc:  # noqa: BLE001
        log.warning("Token validation failed: %s", exc)
        return {
            "principalId": "unauthorized",
            "policyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Action": "execute-api:Invoke",
                        "Effect": "Deny",
                        "Resource": ["arn:aws:execute-api:*:*:*/*"],
                    }
                ],
            },
            "context": {},
        }
