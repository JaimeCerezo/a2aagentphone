"""a2aagentphone -- give an agent a phone number.

Two halves:

* :mod:`a2aagentphone.server` -- the ear. An A2A endpoint that wakes this machine's
  agent when a call comes in.
* :mod:`a2aagentphone.mcp` -- the mouth. An MCP server that lets an agent call
  another one without leaving its conversation.
"""

__version__ = "0.5.3"
