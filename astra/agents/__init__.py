"""Multi-agent system for Astra.

Agents are *task specialists* — declarative, tool-oriented skill buckets the
AgentManager scores for a goal. They are NOT AI providers and NOT a plugin
system: agents know *what kind of work* this is and *which tools* fit; tool
calls run through the ToolRegistry, and AI reasoning goes through the chat
pipeline (Astra AI Gateway + AstraRouter). Chat no longer plans through
specialists — the manager is registered for introspection/routing hints only.
"""
from __future__ import annotations

from .base import SpecialistAgent
from .manager import AgentManager
from .general import GeneralAgent
from .research import ResearchAgent
from .browser import BrowserAgent
from .coding import CodingAgent
from .files import FileAgent
from .web3 import Web3Agent
from .airdrop import AirdropAgent

SPECIALISTS = (GeneralAgent, ResearchAgent, BrowserAgent, CodingAgent,
               FileAgent, Web3Agent, AirdropAgent)

__all__ = ["SpecialistAgent", "AgentManager", "GeneralAgent", "ResearchAgent",
           "BrowserAgent", "CodingAgent", "FileAgent", "Web3Agent",
           "AirdropAgent", "SPECIALISTS"]
