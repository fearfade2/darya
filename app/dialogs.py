"""Conversation namespaces. The Telegram peer and individual group author stay distinct."""
from contextvars import ContextVar

# (Telegram chat, group author or 0 for private chat, context generation)
current_dialog = ContextVar('darya_dialog', default=None)
