"""
title: Freshservice Key
description: Holds your own Freshservice API key for the Freshservice tools. Set it in a chat under Controls > Valves > Tools > Freshservice Key.
"""

# Open WebUI tool for per-user key mode. Install it under Workspace > Tools
# with the ID freshservice_key. It has no functions: it only gives each user a
# private field (a "user valve") for their own key, which the Freshservice MCP
# server reads with the user's login token. The ID and field name must match
# KEY_TOOL_ID and KEY_FIELD in src/freshservice_mcp/per_user.py.

from pydantic import BaseModel, Field


class Tools:
    class UserValves(BaseModel):
        freshservice_api_key: str = Field(
            default="",
            description="Your Freshservice API key (Freshservice > Profile Settings > Your API Key)",
            json_schema_extra={"input": {"type": "password"}},
        )

    def __init__(self):
        pass
