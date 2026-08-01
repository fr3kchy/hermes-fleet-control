import os
import secrets
from fastapi import Header, HTTPException


def require_operator(expected_token: str):
    def verify(authorization: str = Header(default="")) -> None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(token, expected_token):
            raise HTTPException(401, "Operator authentication failed. Supply a valid Bearer token.", headers={"WWW-Authenticate": "Bearer"})
    return verify
