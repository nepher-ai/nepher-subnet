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
- **Review**: The admin team verifies the top submission to confirm no cheating and approves the winner.
- **Reward**: Validators direct all remaining on-chain weight to the approved winner's UID, refreshed hourly.

Period boundaries are defined by Bittensor block numbers, converted to Unix timestamps by the backend.

## Weight Distribution

Multiple tournaments can be active simultaneously. Validators emit a single combined weight vector each cycle:

- Each **non-reward** tournament allocates **1% (fixed)** to its current preliminary leader (if present in the metagraph).
- The **reward-period** tournament's approved winner receives all remaining weight (`1 − 1% × N leaders`).
- Anything unresolved — no active tournaments, no approved winner, or winner not in metagraph — **burns on UID 0**.

Tournament reward periods never overlap by design. Weights are processed in deterministic (ID-sorted) order; identical weight sets committed within 15 minutes are deduplicated and skipped.

The **winner-takes-all** structure is episodic by design — avoiding continuous daily rewards reduces sell pressure and preserves alpha token scarcity.

## Miner Eligibility

- Requires both on-chain registration (present in the metagraph) **and** an agent submission during the contest or submit period.
- If an eligible miner is deregistered before or during evaluation/reward, it still receives its reward. The owner team will notify them to re-register. Hotkey-based emission is mandatory.

---

**Subnet 49 (Nepher)** — Robotics Tournament Subnet
