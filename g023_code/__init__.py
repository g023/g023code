"""
g023 Code — a pure-Python, cost-aware coding agent for the terminal.

One model (DeepSeek V4.1 Flash), one wire (the Anthropic-format Messages
endpoint), one key (K.dat). Single-agent by default; ``/subagents on`` adds
delegation.
"""

__version__ = "1.5.0"
# NOTE: this must not be __name__ — overwriting a package's __name__ breaks
# every `from g023_code import <submodule>` import.
__title__ = "g023 Code"
