try:
    from ._native_approval import resolve_gateway_approval
except ImportError:
    def resolve_gateway_approval(session_key, choice):
        return None
