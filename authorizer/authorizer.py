import json, os, logging, jwt

log = logging.getLogger()
log.setLevel(logging.INFO)

SECRET = os.environ["JWT_SECRET"]
ISS = os.getenv("JWT_ISS")
AUD = os.getenv("JWT_AUD")


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
        payload = jwt.decode(
            token,
            SECRET,
            algorithms=["HS256"],
            issuer=ISS if ISS else None,
            audience=AUD if AUD else None,
            options={"verify_iss": bool(ISS), "verify_aud": bool(AUD)},
        )

        principal = payload.get("sub", "anonymous")

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
                "roles": ",".join(payload.get("roles", [])),
            },
        }

    except Exception as exc:  # noqa: BLE001
        log.warning("JWT validation failed: %s", exc)
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
