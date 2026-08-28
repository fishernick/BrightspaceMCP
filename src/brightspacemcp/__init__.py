"""Brightspace MCP connector.

Exposes the Purdue Brightspace (D2L Valence) LMS as Model Context Protocol
tools: enrollments, grades, course contents, and a merged "what's due" view
reconciled across the content, Quizzes-tool, and calendar feeds.
"""

from brightspacemcp.server import main

__all__ = ["main"]
__version__ = "0.1.0"
