"""State persistence (delegates to the Store on the AppContext)."""

import time


def load_state(ctx):
    return ctx.store.load_state()


def save_state(ctx, state):
    ctx.store.save_state(state)


def update_state(ctx, mutator):
    return ctx.store.update_state(mutator)


def clear_expired_rate_limits(state, now=None):
    """Drop expired rate-limit entries from ``state`` in place.
    Returns True if anything was removed."""
    now_ts = int(now if now is not None else time.time())
    expired = [category for category, retry_until in state.rate_limits.items()
               if int(retry_until) <= now_ts]
    for category in expired:
        del state.rate_limits[category]
    return bool(expired)
