"""`app/adapters/litellm/chat_model.py`: what the real `ChatModels` adapter builds."""
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from app.adapters.litellm.chat_model import OpenAiChatModels


@tool
def lookup(query: str) -> str:
    """Looks something up."""
    return query


def _client(model):
    return model.bound  # a RunnableBinding around the ChatOpenAI


def test_it_is_a_chat_openai_at_the_given_endpoint_with_token_usage_on_and_no_sampling():
    model = OpenAiChatModels().build([lookup], model="chat", base_url="http://gateway.test/v1", api_key="sk-test")

    client = _client(model)
    assert isinstance(client, ChatOpenAI)
    assert client.model_name == "chat" and client.openai_api_base == "http://gateway.test/v1"
    assert client.temperature == 0, "an agent's tool choice must not be a dice roll"
    assert client.stream_usage is True, "without it a streamed turn reports 0 tokens and MAX_TOKENS_PER_TURN never trips"
    assert client.openai_api_key.get_secret_value() == "sk-test"


def test_the_tools_it_is_given_are_bound():
    model = OpenAiChatModels().build([lookup], model="chat", base_url="http://gateway.test/v1", api_key="k")

    assert [t["function"]["name"] for t in model.kwargs["tools"]] == ["lookup"]
