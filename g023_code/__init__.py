"""
g023 Code — Pure-Python DeepSeek V4 powered coding agent
Architecture inspired by Claude Code with strict Subagent-First design.
"""

__version__ = "1.1.0"
# NOTE: this must not be __name__ — overwriting a package's __name__ breaks
# every `from g023_code import <submodule>` import.
__title__ = "g023 Code"
