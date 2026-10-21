Incentive mechanism and business logic for Subnet 49 (Nepher) on Bittensor.

## Tournament Cycle

Each tournament proceeds through five sequential periods:

```
┌────────────┬────────────┬────────────┬────────────┬────────────┐
│  CONTEST   │   SUBMIT   │ EVALUATION │   REVIEW   │   REWARD   │
├────────────┼────────────┼────────────┼────────────┼────────────┤
│ Miners     │ Eligibility│ Validators │ Admin      │ Winner     │
│ submit     │ snapshot   │ evaluate & │ reviews &  │ receives   │
│ agents     │ locked     │ score      │ approves   │ weight     │
└────────────┴────────────┴────────────┴────────────┴────────────┘
```

- **Contest**: Miners train policies locally and submit agents (signed with their Bittensor hotkey).
- **Submit**: The eligible miner list is snapshotted from the metagraph — only registered miners with an active submission are included. The list locks before evaluation. Only the latest agent per miner is scored.
- **Evaluation**: Validators download each eligible agent and run it against standardized Isaac Lab environments via `eval-nav`. Scores are submitted to the tournament backend and aggregated via stake-weighted averaging (ties broken by earliest submission time).
- **Review**: The admin team verifies the top submissions and approves a podium (up to top 3) with a configurable reward split (e.g. 7:2:1).
- **Reward**: Validators split the remaining on-chain weight across approved podium UIDs by their reward shares, refreshed periodically.

Period boundaries are defined by Bittensor block numbers, converted to Unix timestamps by the backend.

## Weight Distribution

Multiple tournaments can be active simultaneously. Validators emit a single combined weight vector each cycle:

- Each **non-reward** tournament allocates **1% (fixed)** to its current preliminary leader (if present in the metagraph).
- The **reward-period** tournament's approved podium receives all remaining weight (`1 − 1% × N leaders`), split by normalized ratios (default **7:2:1** for three places). Missing places redistribute among those present; UIDs not in the metagraph burn their share.
- Anything unresolved — no active tournaments, no approved winner, or no podium UIDs in metagraph — **burns on UID 0**.
- A failed backend or chain lookup is not "unresolved": the validator skips that cycle, leaves on-chain weights unchanged, and retries in 2 minutes.

Tournament reward periods must not overlap, except when a tournament has confirmed
no winner — that tournament's reward window may be overlapped by another. Weights
are processed in deterministic (ID-sorted) order; identical weight sets committed
within 15 minutes are deduplicated and skipped.

The **episodic podium** structure — avoiding continuous daily rewards — reduces sell pressure and preserves alpha token scarcity.

## Miner Eligibility

- Requires both on-chain registration (present in the metagraph) **and** an agent submission during the contest or submit period.
- If an eligible miner is deregistered before or during evaluation/reward, it still receives its reward. The owner team will notify them to re-register. Hotkey-based emission is mandatory.

---

**Subnet 49 (Nepher)** — Robotics Tournament Subnet
