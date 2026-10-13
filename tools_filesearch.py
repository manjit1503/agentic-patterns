from openai import OpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

# ---- Connect to your GPT-5 in Azure (same as your existing code) ----
token_provider = get_bearer_token_provider(DefaultAzureCredential(), "https://ai.azure.com/.default")
client = OpenAI(
    base_url="https://automationandaisphere-7422-resou.services.ai.azure.com/openai/v1",
    api_key=token_provider,
)
MODEL = "gpt-5"
# Create vector store and upload a file
vector_store = client.vector_stores.create(name="policy-docs")
client.vector_stores.files.upload_and_poll(
    vector_store_id=vector_store.id,
    file=open("C:\\Users\\n\\Desktop\\Projects\\MSLearn\\expenses_policy.pdf", "rb")
)
print(f"Vector store created with ID: {vector_store.id}")

# Get response using the file_search tool
response = client.responses.create(
    model=MODEL,
    instructions="You are an AI assistant that provides information from HR policy documents.",
    input="What's the maximum amount I can claim for a taxi ride?",
    tools=[{
        "type": "file_search",
        "vector_store_ids": [vector_store.id]
    }],
    include=["file_search_call.results"]
)
print(response.output_text)