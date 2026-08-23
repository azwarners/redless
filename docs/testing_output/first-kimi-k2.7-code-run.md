# First Kimi K2.7 Code CPU run

This is a sanitized record of an early real-model validation run. It is a single
development result, not a benchmark comparison.

## Environment

- Agent: `mini-swe-agent-slow 2.4.6+slow.1`
- Model: Kimi K2.7 Code, 1T-A32B MoE, UD-IQ4_XS
- Server: llama.cpp, OpenAI-compatible API, one inference slot
- Hardware: HPE ProLiant DL380 Gen9, CPU-only
- Context capacity: 262,144 tokens

## Task

Implement a single-file terminal Hangman game using only the Python standard
library. The task required deterministic `--word WORD` support, clean handling
of invalid and repeated guesses, quit support, win/loss behavior, and scripted
validation of invalid/repeated input, a win, and a loss.

## Result

The agent created `hangman.py` and completed the requested validation:

- `python3 -m py_compile hangman.py` passed.
- Invalid and repeated guesses were rejected cleanly.
- A deterministic winning case using `--word code` printed the win message.
- A deterministic losing case using `--word code` printed the loss message.
- A manual interactive game also completed successfully.
- The agent finished with a normal final response; it did not replay the final
  inference request.
- The new file remained untracked; the agent did not stage, commit, reset, or
  otherwise change Git state.

## Timing

| Metric | Result |
| --- | ---: |
| Model calls | 7 |
| Tool actions | 6 |
| Model elapsed time | about 75 minutes |
| Deterministic tool time | under 1 second |
| Initial request | about 24 minutes 40 seconds |
| Slowest later request | about 23 minutes 9 seconds |
| Decode throughput | about 0.8–0.9 tokens/second |

## llama.cpp observations

The server retained the same slot across follow-up requests. Longest-common-prefix
similarity ranged from approximately 0.79 to 0.98, with `f_keep = 1.000` on the
observed follow-up requests. No request was truncated.

This supports the fork's current approach: keep the trajectory stable for server
cache reuse, avoid automatic replay of ambiguous inference, and spend cheap local
computation aggressively to reduce the number and length of model generations.

The original terminal and server logs are intentionally not included because they
contained local usernames, hostnames, paths, launch details, and security-sensitive
server configuration warnings.
