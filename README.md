# werewolf-agent

Heuristic Werewolf agent for make.fun competition `werewolf-agent-01`.
Pure Python 3 stdlib: Bayesian-ish suspicion scoring from the public record
(votes, accusations, seer claims and verified findings), role policies for
werewolf / seer / doctor / villager, deterministic from `MAKEFUN_TRIAL_SEED`.
No model calls. Entrypoint: `main.py`.

`sim.py` is a local mini-engine used to exercise the decision functions.

License: MIT.
