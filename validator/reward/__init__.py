"""Reward module."""

__all__ = ["WeightSetter"]


def __getattr__(name):
    # Lazy import so importing lightweight submodules (e.g. ``distribution``)
    # does not pull in bittensor via ``weight_setter``.
    if name == "WeightSetter":
        from validator.reward.weight_setter import WeightSetter

        return WeightSetter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

