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


# Get response using the code_interpreter tool
response = client.responses.create(
    model=MODEL,
    instructions="You are an AI assistant that provides information. Use the python tool to run code for math problems.",
    input="What is the numerical value of square  of matrix {(1,2)(3,4)}?",
    tools=[{"type": "code_interpreter",
            "container": {"type": "auto"}}]
)
print(response.output_text)
print(response.reasoning)