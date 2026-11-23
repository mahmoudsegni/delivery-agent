"""LiteLLM provider implementation with multi-provider fallback support."""

import logging
from typing import Any, TypeVar

import litellm
from pydantic import BaseModel

from app.config import Settings
from app.llm.base import (
    LLMAuthenticationError,
    LLMConnectionError,
    LLMError,
    LLMProvider,
    LLMRateLimitError,
)

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.

    Supports automatic fallback between OpenAI, Anthropic, and Azure OpenAI
    based on configured API keys. Only providers with valid API keys are included
    in the fallback chain.

    Features:
        - Automatic provider detection from API keys
        - Fallback chain for resilience
        - Built-in retry with exponential backoff
        - Structured output with Pydantic models
    """

    def __init__(self, settings: Settings):
        """
        Initialize the LiteLLM provider.

        Args:
            settings: Application settings containing API keys and configuration.
        """
        self._settings = settings
        self._default_model = settings.llm_model
        self._timeout = 60.0  # Default timeout in seconds
        self._model_list = self._build_model_list()
        self._router = self._create_router() if self._model_list else None

        # Configure litellm global settings
        litellm.set_verbose = settings.debug
        litellm.drop_params = True  # Drop unsupported params instead of erroring

    def _build_model_list(self) -> list[dict[str, Any]]:
        """
        Build the model list for LiteLLM Router based on available API keys.

        Only includes providers that have valid API keys configured.
        """
        model_list = []

        # OpenAI models
        if self._settings.openai_api_key:
            model_list.append({
                "model_name": "gpt-4",
                "litellm_params": {
                    "model": "openai/gpt-4",
                    "api_key": self._settings.openai_api_key,
                },
            })
            model_list.append({
                "model_name": "gpt-4-turbo",
                "litellm_params": {
                    "model": "openai/gpt-4-turbo-preview",
                    "api_key": self._settings.openai_api_key,
                },
            })
            model_list.append({
                "model_name": "gpt-3.5-turbo",
                "litellm_params": {
                    "model": "openai/gpt-3.5-turbo",
                    "api_key": self._settings.openai_api_key,
                },
            })

        # Anthropic models
        if self._settings.anthropic_api_key:
            model_list.append({
                "model_name": "claude-3-opus",
                "litellm_params": {
                    "model": "anthropic/claude-3-opus-20240229",
                    "api_key": self._settings.anthropic_api_key,
                },
            })
            model_list.append({
                "model_name": "claude-3-sonnet",
                "litellm_params": {
                    "model": "anthropic/claude-3-sonnet-20240229",
                    "api_key": self._settings.anthropic_api_key,
                },
            })
            # Add Claude as fallback for gpt-4 if OpenAI not available
            if not self._settings.openai_api_key:
                model_list.append({
                    "model_name": "gpt-4",
                    "litellm_params": {
                        "model": "anthropic/claude-3-sonnet-20240229",
                        "api_key": self._settings.anthropic_api_key,
                    },
                })

        # Azure OpenAI models
        if self._settings.azure_api_key and self._settings.azure_api_base:
            model_list.append({
                "model_name": "azure-gpt-4",
                "litellm_params": {
                    "model": "azure/gpt-4",
                    "api_key": self._settings.azure_api_key,
                    "api_base": self._settings.azure_api_base,
                },
            })
            # Add Azure as fallback for gpt-4 if others not available
            if not self._settings.openai_api_key and not self._settings.anthropic_api_key:
                model_list.append({
                    "model_name": "gpt-4",
                    "litellm_params": {
                        "model": "azure/gpt-4",
                        "api_key": self._settings.azure_api_key,
                        "api_base": self._settings.azure_api_base,
                    },
                })

        return model_list

    def _create_router(self) -> litellm.Router | None:
        """Create LiteLLM Router with fallback configuration."""
        if not self._model_list:
            return None

        return litellm.Router(
            model_list=self._model_list,
            num_retries=3,
            retry_after=1,  # Exponential backoff starting at 1 second
            timeout=self._timeout,
            fallbacks=[],  # Can add fallback chains here
            set_verbose=self._settings.debug,
        )

    @property
    def name(self) -> str:
        """Provider name identifier."""
        return "litellm"

    @property
    def default_model(self) -> str:
        """Default model to use for completions."""
        return self._default_model

    @property
    def is_available(self) -> bool:
        """Check if at least one provider is configured."""
        return len(self._model_list) > 0

    def _get_available_providers(self) -> list[str]:
        """Get list of configured provider names."""
        providers = []
        if self._settings.openai_api_key:
            providers.append("openai")
        if self._settings.anthropic_api_key:
            providers.append("anthropic")
        if self._settings.azure_api_key and self._settings.azure_api_base:
            providers.append("azure")
        return providers

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        response_format: type[T] | None = None,
        **kwargs: Any,
    ) -> str | T:
        """
        Generate a completion using LiteLLM.

        Uses the Router for automatic fallback and retry handling.
        Falls back to direct acompletion if Router is not available.

        Args:
            messages: List of message dictionaries.
            model: Model name override.
            response_format: Optional Pydantic model for structured output.
            **kwargs: Additional parameters passed to LiteLLM.

        Returns:
            String response or parsed Pydantic model.

        Raises:
            LLMError: On provider errors.
        """
        target_model = model or self._default_model

        if not self.is_available:
            raise LLMError(
                "No LLM providers configured. Set at least one API key "
                "(OPENAI_API_KEY, ANTHROPIC_API_KEY, or AZURE_API_KEY).",
                provider=self.name,
            )

        # Build completion kwargs
        completion_kwargs: dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "timeout": kwargs.pop("timeout", self._timeout),
            **kwargs,
        }

        # Add response_format for structured output if supported
        if response_format is not None:
            # Use JSON mode for structured output
            completion_kwargs["response_format"] = {"type": "json_object"}

        try:
            logger.debug(f"Calling LLM with model={target_model}, messages={len(messages)}")

            # Use Router if available, otherwise direct call
            if self._router is not None:
                response = await self._router.acompletion(**completion_kwargs)
            else:
                response = await litellm.acompletion(**completion_kwargs)

            # Extract content from response
            content = response.choices[0].message.content

            if content is None:
                raise LLMError("LLM returned empty content", provider=self.name, model=target_model)

            logger.debug(f"LLM response received, length={len(content)}")

            # Parse structured output if requested
            if response_format is not None:
                try:
                    return response_format.model_validate_json(content)
                except Exception as e:
                    raise LLMError(
                        f"Failed to parse LLM response as {response_format.__name__}: {e}",
                        provider=self.name,
                        model=target_model,
                    ) from e

            return content

        except litellm.AuthenticationError as e:
            raise LLMAuthenticationError(
                f"Authentication failed: {e}",
                provider=self.name,
                model=target_model,
            ) from e
        except litellm.RateLimitError as e:
            raise LLMRateLimitError(
                f"Rate limit exceeded: {e}",
                provider=self.name,
                model=target_model,
            ) from e
        except litellm.APIConnectionError as e:
            raise LLMConnectionError(
                f"Connection error: {e}",
                provider=self.name,
                model=target_model,
            ) from e
        except litellm.APIError as e:
            raise LLMError(
                f"API error: {e}",
                provider=self.name,
                model=target_model,
            ) from e
        except Exception as e:
            # Check if it's one of our custom errors being re-raised
            if isinstance(e, LLMError):
                raise
            raise LLMError(
                f"Unexpected error: {e}",
                provider=self.name,
                model=target_model,
            ) from e
