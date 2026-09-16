# Dockerfile Optimizer Explainability

## Decision Reasoning
The agent parses a Dockerfile into stages and logical instructions, then runs a fixed registry of deterministic rules over that model, each carrying a severity that callers gate on. An optional advisory layer asks a language model only the judgement calls the deterministic engine explicitly refuses, and its answers are labelled, never applied, and discarded unless they respond to a question the engine asked.

## Data Inputs
The only required input is the raw text of the target Dockerfile, read from a path supplied on the command line, shaped further by an optional TOML configuration file. The `suggest` command additionally reads a Gemini API key from the environment and sends the Dockerfile and the engine's open questions to that API.

## Known Limitations
The `analyze` and `refactor` commands perform no network access and never resolve a registry, so they cannot verify that a pinned digest exists or report CVEs affecting a base image. Nothing in the agent executes containers, so runtime failures and true layer sizes remain outside what static analysis can observe, and any model suggestion is an opinion that has not been tested against a real build.
