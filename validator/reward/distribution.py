"""Pure weight-distribution math (no bittensor / network dependencies).

Separated from ``weight_setter`` so the allocation logic is unit-testable
without importing heavy chain dependencies.
"""

from typing import Iterable, Optional


# Fraction of total weight allocated to each non-reward tournament's
# preliminary leader.
LEADER_WEIGHT_FRACTION = 0.01

# UID that receives unallocated / burned weight.
DEFAULT_BURN_UID = 0


def compute_weight_distribution(
    leader_uids: Iterable[Optional[int]],
    reward_winner_uid: Optional[int],
    burn_uid: int = DEFAULT_BURN_UID,
    leader_fraction: float = LEADER_WEIGHT_FRACTION,
) -> dict[int, float]:
    """Build the combined ``{uid: weight}`` distribution.

    Args:
        leader_uids: One entry per non-reward tournament. Each is the UID of
            that tournament's preliminary leader, or ``None`` to skip (leader
            unknown / not in the metagraph). The skipped 1% rolls into the
            remainder.
        reward_winner_uid: UID of the single reward-period winner, or ``None``
            to burn the remainder on ``burn_uid``.
        burn_uid: UID that receives unallocated weight (default 0).
        leader_fraction: Fixed fraction per leader (default 1%).

    Returns:
        A normalized ``{uid: weight}`` map summing to 1.0. Duplicate UIDs
        (leader == winner == burn) are merged by addition.
    """
    weight_map: dict[int, float] = {}
    fixed_total = 0.0

    for uid in leader_uids:
        if uid is None:
            continue
        weight_map[uid] = weight_map.get(uid, 0.0) + leader_fraction
        fixed_total += leader_fraction

    remainder = max(0.0, 1.0 - fixed_total)

    recipient = reward_winner_uid if reward_winner_uid is not None else burn_uid
    weight_map[recipient] = weight_map.get(recipient, 0.0) + remainder

    total = sum(weight_map.values())
    if total <= 0:
        return {burn_uid: 1.0}
    if abs(total - 1.0) > 1e-9:
        weight_map = {uid: w / total for uid, w in weight_map.items()}

    return weight_map
