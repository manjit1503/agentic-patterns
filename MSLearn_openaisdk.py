from openai import OpenAI
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

# ---- Connect to your GPT-5 in Azure (same as your existing code) ----
token_provider = get_bearer_token_provider(DefaultAzureCredential(), "https://ai.azure.com/.default")
client = OpenAI(
    base_url="https://automationandaisphere-7422-resou.services.ai.azure.com/openai/v1",
    api_key=token_provider,
)
MODEL = "gpt-5"


# =====================================================================
# WAY 1: CHAT COMPLETIONS
# The AI forgets everything after each answer.
# So YOU keep a list of the whole conversation and send it every time.
# =====================================================================
print("========== CHAT COMPLETIONS ==========")

conversation = [
    {"role": "system", "content": "You are a stock trader."},
    {"role": "user", "content": "Which is better: WDC or AMAT? Answer in 2 lines."},
]

answer1 = client.chat.completions.create(model=MODEL, messages=conversation)
print("Answer 1:", answer1.choices[0].message.content)

# Add the AI's answer and your next question to the list...
#conversation.append({"role": "assistant", "content": answer1.choices[0].message.content})
conversation=[{"role": "user", "content": "How would you split $10k between them?"}]

# ...and send the WHOLE list again
answer2 = client.chat.completions.create(model=MODEL, messages=conversation)
print("Answer 2:", answer2.choices[0].message.content)


# =====================================================================
# WAY 2: RESPONSES
# Azure remembers the conversation for you.
# You only send the NEW question plus the ID of the last answer.
# =====================================================================
print("\n========== RESPONSES ==========")

reply1 = client.responses.create(
    model=MODEL,
    instructions="You are a stock trader.",
    input="Which is better: WDC or AMAT? Answer in 2 lines.",
)
print("Answer 1:", reply1.output_text)

reply2 = client.responses.create(
    model=MODEL,
    instructions="You are a stock trader.",
    input="How would you split $10k between them?",   # only the new question
    previous_response_id=reply1.id,                    # "continue from this answer"
)
print("Answer 2:", reply2.output_text)