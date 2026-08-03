"""Temporary probe: fire a knowledge-advisory search through the BAP caller."""
import asyncio
import json

from agents.tools.knowledge_advisory import knowledge_advisory

result = asyncio.run(knowledge_advisory("What fertilizer should I use for maize?"))
print("=== TOOL OUTPUT ===")
print(result[:4000])
