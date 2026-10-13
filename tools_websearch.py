from openai import OpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

# ---- Connect to your GPT-5 in Azure (same as your existing code) ----
token_provider = get_bearer_token_provider(DefaultAzureCredential(), "https://ai.azure.com/.default")
client = OpenAI(
    base_url="https://automationandaisphere-7422-resou.services.ai.azure.com/openai/v1",
    api_key=token_provider,
)
MODEL = "gpt-5"

from openai import OpenAI

# Get response using the web_search tool
response = client.responses.create(
    model=MODEL,
    instructions="You are an AI assistant. Use web search when current information is required.",
    input="How tokenisation will help US stock market?",
    tools=[{"type": "web_search"}]
)

print(response.output_text)