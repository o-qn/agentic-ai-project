"""Select the assessment provider; embeddings remain local."""
from .ollama_client import Ollama


def create_model(config):
    if config.provider == 'agentrouter':
        from .agentrouter_client import AgentRouter
        return AgentRouter(config)
    return Ollama(config)
