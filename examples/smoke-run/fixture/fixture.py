def clamp_count(value, limit):
    """Clamp an integer to the inclusive range zero..limit."""
    if limit < 0:
        raise ValueError("limit must be non-negative")
    return min(max(value, 0), limit)
