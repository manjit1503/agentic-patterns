"""
Talk to the EXISTING agent 'foundry-demo' (created in the Foundry portal).

pip install --upgrade azure-ai-projects azure-identity openai
az login
"""

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

# Project endpoint: Foundry portal -> your project -> Overview
# (the agent's endpoint starts with this same URL)
PROJECT_ENDPOINT = "https://automationandaisphere-7422-resou.services.ai.azure.com/api/projects/automationandaisphere-7422"
AGENT_NAME = "foundry-demo"   # the Agent ID from the portal popup

project = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=DefaultAzureCredential())
client = project.get_openai_client()

# Point every call at your agent (it already knows its model + instructions)
AGENT = {"agent_reference": {"name": AGENT_NAME, "type": "agent_reference"}}

# Turn 1
reply1 = client.responses.create(
    input="Which is better: WDC or AMAT? Answer in 2 lines.",
    extra_body=AGENT,
)
print("Agent:", reply1.output_text)

# Turn 2: same memory trick as before
reply2 = client.responses.create(
    input="How would you split $10k between them?",
    previous_response_id=reply1.id,
    extra_body=AGENT,
)
print("Agent:", reply2.output_text)