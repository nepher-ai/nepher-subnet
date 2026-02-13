Incentive mechanism and business logic for Subnet 49 (Nepher) on Bittensor.

## Tournament Cycle

Each tournament proceeds through five sequential periods:

```
┌─────────────┬─────────────┬─────────────┬─────────────┬─────────────┐
│   CONTEST   │   SUBMIT    │ EVALUATION  │   REVIEW    │   REWARD    │
│   PERIOD    │   PERIOD    │   PERIOD    │   PERIOD    │   PERIOD    │
├─────────────┼─────────────┼─────────────┼─────────────┼─────────────┤
│ Miners      │ Eligibility │ Validators  │ Admin       │ Winner gets │
│ submit      │ snapshot    │ evaluate    │ reviews &   │ all weight  │
│ agents      │ locked      │ agents      │ approves    │             │
└─────────────┴─────────────┴─────────────┴─────────────┴─────────────┘
```

- **Contest Period**: Miners train policies locally and submit agents to the tournament backend. Submissions are signed with the miner's Bittensor hotkey.
- **Submit Period**: The eligible miner list is snapshotted from the metagraph — only miners who are registered on-chain *and* have submitted an agent are included. The snapshot is then locked. Only the latest agent per miner is scored.
- **Evaluation Period**: Validators download each eligible agent, install its task module, and run it against standardized Isaac Lab environments via `eval-nav`. Scores are submitted back to the tournament backend.
- **Review Period**: The admin team reviews aggregated scores and verifies the top submission to confirm no cheating before approving the winner.
- **Reward Period**: Validators set all on-chain weight to the approved winner's UID. Weights are refreshed hourly for the duration of the period.

All period boundaries are defined by Bittensor block numbers, converted to Unix timestamps by the backend.

## Incentive Mechanism

### Winner-Takes-All

Only a single top-performing miner receives weight (and thus emissions) per tournament.

### Scoring

Validator scores are aggregated using stake-weighted averaging. Each validator's score for an agent is weighted by that validator's stake on the subnet. If no validator has stake, a simple average is used as fallback. Rankings are determined by aggregated score descending, with ties broken by earliest submission time.

### Emission Distribution

Outside the reward period — during contest, submit period, evaluation, and review — validators set weight to UID 0 (burn). Emissions are only directed to the winner during the reward period. After the reward period ends, weight returns to UID 0.

The emission schedule is sparse and episodic. Continuous daily rewards are avoided to reduce sell pressure and preserve alpha token scarcity. Alpha is treated as prize capital, not recurring income.

### Performance Thresholds

If no miner meets the threshold, or if the admin does not approve a winner, all weight is directed to UID 0 (burn) and no emissions are distributed.

## Miner Eligibility

- Eligibility requires both on-chain registration (present in the metagraph) and an agent submission during the contest/submit period. The eligible miner list is periodically synced during the submit period and locked before evaluation begins.
- If an eligible miner is deregistered before or during evaluation/reward, it still receives its reward. The owner team will notify them to re-register. Hotkey-based emission is mandatory.

---

**Subnet 49 (Nepher)** — Robotics Tournament Subnet
