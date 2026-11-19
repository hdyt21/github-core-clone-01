"""Pure-Python git forge for make.fun github-core-clone-01.

A single process, stdlib only. It serves git smart HTTP, REST v3, GraphQL
and MCP from one in-memory deterministic store. No timestamps and no
randomness in any stored state, so two runs on the same instance leave
identical state behind.
"""
