# Both command-line and MCP entry points use this connection setup to stay consistent.
"""Load a local credential and open exactly the provider selected by the host."""
# A context manager closes the HTTP client when its indented with-block finishes.
from contextlib import contextmanager
# Path finds .env beside this file, even if PowerShell starts in a different folder.
from pathlib import Path
# dotenv_values reads configuration without copying the key into printed output.
from dotenv import dotenv_values
# Each backend translates the same planner request into its provider's API format.
from model_planner import GeminiBackend, OpenAIBackend

# This fixed mapping prevents accidentally sending one provider's key to the other.
API_KEY_NAMES = {"gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY"}


# The reflector reviews the actor's trajectory, so it must be a different model (Lecture 3:
# self-critique by the same model is prone to confirmation bias; a second model reduces shared
# blind spots). Same provider is fine; same provider AND model is refused unless allowed.
DEFAULT_REFLECT_MODEL = {"openai": "gpt-5.5"}


def check_independent_reflector(actor_provider, actor_model, reflect_provider, reflect_model,
                                allow_same=False):
    if not reflect_model:
        raise ValueError("Choose a reflector model (it must differ from the acting model)")
    if (actor_provider, actor_model) == (reflect_provider, reflect_model) and not allow_same:
        raise ValueError("The reflector must be a different model from the acting model; "
                         "pass a different reflect model or explicitly allow the same one")


# @contextmanager makes this generator usable as: with open_backend(...) as backend.
@contextmanager
# Callers choose a provider and model; credentials are always read locally.
def open_backend(provider, model):
    # Validate the provider before reading credentials or constructing an HTTP client.
    if provider not in API_KEY_NAMES:
        # This message contains only our own fixed text, never credential values.
        raise ValueError("Provider must be gemini or openai")
    # Select only the credential belonging to the explicitly chosen provider.
    key_name = API_KEY_NAMES[provider]
    # Load the private file next to this module; never log its contents.
    key = dotenv_values(Path(__file__).resolve().parent / ".env").get(key_name)
    # A missing or blank value should produce an actionable local error before any API call.
    if not key or not key.strip():
        # The variable name is safe to display; its secret value is never included.
        raise ValueError(f"Missing {key_name} in the project's .env")
    # Import only the SDK needed for the selected provider.
    if provider == "openai":
        # OpenAI is installed separately from google-genai.
        from openai import OpenAI
        # Pin the official API destination and disable automatic retries to bound spending.
        with OpenAI(api_key=key, base_url="https://api.openai.com/v1",
                    timeout=20.0, max_retries=0) as client:
            # Give the caller a planner-compatible backend while the HTTP client is open.
            yield OpenAIBackend(client, model)
    # Preserve the original Gemini behavior when a caller explicitly selects that provider.
    else:
        # These imports are unnecessary when the worker uses OpenAI.
        from google import genai
        # Gemini's timeout is measured in milliseconds rather than seconds.
        from google.genai import types
        # One attempt keeps Gemini calls bounded just like the OpenAI configuration.
        with genai.Client(api_key=key, vertexai=False,
                          http_options=types.HttpOptions(timeout=20_000,
                              retry_options=types.HttpRetryOptions(attempts=1))) as client:
            # The with-block closes this client when the worker returns or raises an error.
            yield GeminiBackend(client, model)
