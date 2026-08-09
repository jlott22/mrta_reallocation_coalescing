# The copied legacy CLI exposes Bayesian and Top-K campaign selectors.  It is
# intentionally quarantined in ``allocator_replay.cli`` for audit history;
# module execution enters only the coalescing-specific command surface.
from .coalescing.cli import main


if __name__ == "__main__":
    main()
